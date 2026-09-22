"""Drive the real pdpop UI; never reconstruct purchase calls or handle passwords."""
from __future__ import annotations

from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from policy import Decision, matches_movie, select_files

BASE = 'https://new.pdpop.com'
SNAPSHOT_JS = r"""() => ({
    title: document.title.replace(/\s*\|.*$/, '').trim(),
    logged_in: typeof getCookie === 'function' && !!getCookie('uid'),
    fixed: typeof isMemberFixedDownload === 'number' ? isMemberFixedDownload : null,
    cine: typeof isBoardCine === 'number' ? isBoardCine : null,
    category: typeof cate1 === 'string' ? cate1 : null,
    code: typeof code === 'string' ? code : null,
    admin: typeof isAdmin === 'string' ? isAdmin : null,
    files: Array.from(document.querySelectorAll('input[name="fileList"]')).map(el => ({
        id: el.dataset.fileno || '',
        name: el.closest('ul')?.querySelector('.fileRealName')?.textContent.trim() || '',
        folder: el.dataset.folder || '',
        size: /^\d+$/.test(el.dataset.size || '') ? Number(el.dataset.size) : null,
        cine: el.dataset.iscine ?? null,
        disabled: el.disabled,
        checked: el.checked
    }))
})"""


class Halt(RuntimeError):
    """A condition requiring human review; never retry a possibly sent purchase."""


def is_purchase_url(url: str) -> bool:
    u = urlsplit(url)
    if not (u.hostname == 'pdpop.com' or (u.hostname or '').endswith('.pdpop.com')):
        return False
    target = u.path.lower() + '?' + u.query.lower()
    return any(word in target for word in ('/download/', 'purchase', 'buycontent', 'enroll.php', 'rewardgift', 'coupondownload', 'file_download'))


def authorized_request(url: str, permit: dict | None) -> bool:
    if not permit:
        return False
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname != 'cgi.pdpop.com' or u.path != '/download/':
        return False
    q = parse_qs(u.query, keep_blank_values=True)
    allowed_keys = {'szMode', 'szNo', 'szCode', 'szdownMode', 'szCoupon', 'szDown',
                    'szFile', 'szFolder', 'hash', 'my_multi_port', 'my_multi_time', '_'}
    if not set(q) <= allowed_keys or any(len(v) != 1 for v in q.values()):
        return False
    if 'my_multi_port' in q and q['my_multi_port'] != ['45674']:
        return False
    expected = {'szMode': 'file_download_new', 'szNo': permit['board'],
                'szCode': permit['code'], 'szdownMode': 'normal',
                'szCoupon': '', 'szDown': '0'}
    if any(q.get(k) != [v] for k, v in expected.items()):
        return False
    files = q.get('szFile', [''])[0].split('*')
    folders = q.get('szFolder', [''])[0].split('*')
    return (len(q.get('szFile', [])) == 1 and len(files) == len(set(files))
            and set(files) == set(permit['files'])
            and len(q.get('szFolder', [])) == 1
            and set(folders) == set(permit['folders']))


class SiteBrowser:
    def __init__(self, profile: Path, log, stop):
        self.profile, self.log, self.stop = profile, log, stop
        self.playwright = self.context = self.page = None
        self.permit = None
        self.sent = False
        self.problem = ''
        self.response_status = None

    def open(self):
        if self.context:
            try:
                if self.page.is_closed():
                    self.page = self.context.new_page()
                self.page.bring_to_front()
                return
            except Exception:
                self.close()
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.profile), channel='msedge', headless=False,
                viewport={'width': 1200, 'height': 850},
                accept_downloads=False, service_workers='block',
            )
        except Exception as exc:
            self.playwright.stop()
            self.playwright = None
            raise Halt('Microsoft Edge를 시작하지 못했습니다. Edge 설치 여부를 확인하고 도구를 한 번만 실행하세요.') from exc
        # Route all pages (including popups); no charge request is permitted while browsing.
        self.context.route('**/*', self._route)
        self.context.on('page', self._attach_page)
        for p in self.context.pages:
            self._attach_page(p)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(15000)
        self.page.goto(BASE + '/list/F_02', wait_until='domcontentloaded', timeout=45000)
        self.log('전용 Edge 창이 열렸습니다. 이 창에서 피디팝에 직접 로그인하세요. 기존 Edge 로그인은 가져오지 않습니다.')

    def _attach_page(self, page):
        page.on('dialog', self._dialog)
        page.on('response', self._response)
        page.on('download', lambda item: item.cancel())

    def _dialog(self, dialog):
        # Decline ALL confirms/prompts, not merely those containing a guessed price word.
        kind = dialog.type
        dialog.dismiss()
        self.problem = '사이트 알림 또는 확인창을 자동 취소했습니다. 전용 Edge 창에서 상태를 확인하세요.'
        self.log(f'안전 중지: {kind} 대화상자. 구매/쿠폰/포인트 사용을 승인하지 않았습니다.')
        self.stop.set()

    def _response(self, response):
        if self.sent and is_purchase_url(response.url):
            self.response_status = response.status

    def _route(self, route):
        request = route.request
        if is_purchase_url(request.url):
            if (self.stop.is_set() or self.sent or request.method != 'GET'
                    or not authorized_request(request.url, self.permit)):
                route.abort('blockedbyclient')
                self.problem = '허가되지 않은 구매 요청을 차단했습니다.'
                self.stop.set()
                return
            # Consume one-time permission BEFORE the request leaves the browser.
            self.sent = True
            self.permit = None
        route.continue_()

    def reset_job(self):
        self.problem = ''
        self.permit = None
        self.sent = False
        self.response_status = None

    def ensure_login(self):
        if not self.context or not self.page or self.page.is_closed():
            self.open()
        if not self.page.evaluate("() => typeof getCookie === 'function' && !!getCookie('uid')"):
            raise Halt('먼저 도구가 연 Edge 창에서 로그인한 후 다시 실행하세요.')

    def search(self, movie: dict, pages: int = 2) -> list[dict]:
        self.ensure_login()
        query = urlencode({'searchKeyword': 'subject', 'searchWord': movie['title']})
        self.page.goto(BASE + '/list/F_02?' + query, wait_until='domcontentloaded', timeout=45000)
        self.page.wait_for_function("() => typeof callFileShareLoadList === 'function' && typeof listAbortController !== 'undefined' && listAbortController === null", timeout=45000)
        # Use the site's existing fixed-plan tab (not a fabricated endpoint).
        tab = self.page.locator('a.change-list[data-listmode="noneCineList"]')
        if tab.count() != 1:
            raise Halt('정액권가능 탭을 찾을 수 없습니다. 사이트 구조가 변경되었을 수 있습니다.')
        tab.click()
        self.page.wait_for_function("() => typeof onlyNoneCine !== 'undefined' && String(onlyNoneCine) === 'Y' && listAbortController === null", timeout=45000)
        # Gallery mode does not expose the same stable subject IDs.
        self.page.evaluate('() => callFileShareLoadList(1, nowListMode, "list")')
        candidates = []
        seen = set()
        for page_no in range(1, pages + 1):
            if self.stop.is_set():
                break
            if page_no > 1:
                total = self.page.evaluate('() => totalPage')
                if page_no > total:
                    break
                self.page.evaluate('(n) => callFileShareLoadList(n, nowListMode, "list")', page_no)
                if self.page.evaluate('() => nowPage') != page_no:
                    raise Halt('검색 결과 페이지를 확인할 수 없어 중지했습니다.')
            rows = self.page.locator('#layerList [id^="subject-"]').evaluate_all("els => els.map(el => ({id: el.id.slice(8), title: el.getAttribute('alt') || el.textContent.trim()}))")
            for row in rows:
                if re.fullmatch(r'\d+', row['id']) and row['id'] not in seen and matches_movie(row['title'], movie):
                    seen.add(row['id'])
                    candidates.append(row)
            self.wait(1)
        # Keep deterministic site order, bounded to limit load on the service.
        return candidates[:6]

    def inspect(self, board: str, movie: dict) -> tuple[dict, Decision]:
        if not re.fullmatch(r'\d+', board):
            raise Halt('잘못된 게시물 번호입니다.')
        self.page.goto(BASE + '/view/' + board, wait_until='domcontentloaded', timeout=45000)
        self.page.wait_for_function("() => typeof selectFileCalculate === 'function'", timeout=20000)
        snapshot = self.page.evaluate(SNAPSHOT_JS)
        if not matches_movie(snapshot['title'], movie):
            return snapshot, Decision(False, '상세 제목과 영화·개봉 연도가 일치하지 않습니다.', [])
        decision = select_files(snapshot)
        return snapshot, decision

    def submit(self, board: str, movie: dict, expected: Decision):
        """Caller must persist an uncertain attempt BEFORE entering this method."""
        self.reset_job()
        snapshot = self.page.evaluate(SNAPSHOT_JS)
        fresh = select_files(snapshot)
        if not fresh.allowed or not matches_movie(snapshot['title'], movie):
            raise Halt('실행 직전 정액권/영화 정보 검증에 실패했습니다.')
        if fresh.files != expected.files or snapshot.get('admin') != '':
            raise Halt('파일 목록 또는 계정 상태가 변경되어 중지했습니다.')
        ids = [f['id'] for f in fresh.files]
        # Folder selections MUST be cleared. The purchase handler sends both folder
        # and file fields; a checked parent folder could include excluded files.
        self.page.evaluate(r"""ids => {
            for (const el of document.querySelectorAll('input[name="folder"], input[name="folderBottom"], #checkAll, #checkAllBottom')) el.checked = false;
            for (const el of document.querySelectorAll('input[name="fileList"], input[name="fileListBottom"]')) {
                el.checked = ids.includes(el.dataset.fileno);
            }
            selectFileCalculate();
        }""", ids)
        check = self.page.evaluate(r"""() => ({
            fixed: typeof isMemberFixedDownload === 'number' ? isMemberFixedDownload : null,
            cine: selectCineFile,
            files: Array.from(document.querySelectorAll('input[name="fileList"]:checked')).map(e=>e.dataset.fileno),
            folders: Array.from(document.querySelectorAll('input[name="folder"]:checked')).map(e=>e.value)
        })""")
        if check != {'fixed': 1, 'cine': 0, 'files': ids, 'folders': []}:
            raise Halt('선택 파일 또는 정액권 재검증에 실패했습니다.')
        if self.stop.is_set():
            raise Halt('사용자가 중지했습니다.')
        self.permit = {'board': board, 'code': snapshot['code'], 'files': ids, 'folders': ['']}
        button = self.page.locator('button[onclick="downloadFile(\'firstDownload\');"], button[onclick="downloadFile(\'firstDownload\')"]')
        try:
            if button.count() != 1:
                raise Halt('공식 다운로드 버튼을 정확히 식별하지 못했습니다.')
            button.click(timeout=20000)
            deadline = time.monotonic() + 18
            while not self.sent and not self.problem and not self.stop.is_set() and time.monotonic() < deadline:
                self.page.wait_for_timeout(250)
            if self.problem:
                raise Halt(self.problem)
            if not self.sent:
                raise Halt('전용 다운로더 호출을 확인하지 못했습니다. Edge의 로컬 네트워크 접근 권한과 다운로더 실행 상태를 확인하세요. 이 항목은 자동 재시도하지 않습니다.')
            self.wait(2)
            if self.problem or self.stop.is_set():
                raise Halt(self.problem or '사용자가 중지했습니다. 요청이 전송되었을 수 있으므로 재요청하지 않습니다.')
            if self.response_status is not None and not 200 <= self.response_status < 300:
                raise Halt('다운로드 요청의 서버 응답이 정상적이지 않습니다. 자동 재시도하지 않습니다.')
        finally:
            self.permit = None

    def wait(self, seconds: float):
        # Pump Playwright events even while waiting on native file transfer.
        until = time.monotonic() + seconds
        while time.monotonic() < until and not self.stop.is_set():
            self.page.wait_for_timeout(200)

    def close(self):
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.context = self.playwright = self.page = None

"""Local-only Windows control panel and serialized automation worker."""
from __future__ import annotations

import base64
from collections import deque
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser

from browser import Halt, SiteBrowser
from native import change_destination
from policy import CATALOG, GIB, validate_destination

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'


def acquire_instance_lock(data: Path):
    """Keep one owner of the journal even if a launcher exits unexpectedly."""
    data.mkdir(parents=True, exist_ok=True)
    handle = (data / 'instance.lock').open('a+b')
    handle.seek(0)
    if not handle.read(1):
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another assistant is already running. Close it before starting again.') from None
    return handle


def atomic_json(path: Path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def present_files(destination: Path, expected: list[dict]) -> bool:
    """Bounded scan, no junction traversal. Exact names AND byte counts, not progress guesses."""
    wanted = {(f['name'].casefold(), f['size']) for f in expected}
    found = set()
    checked = 0
    for root, dirs, files in os.walk(destination, followlinks=False):
        parent = Path(root)
        dirs[:] = [d for d in dirs if not (parent / d).is_symlink()
                   and not (hasattr(Path, 'is_junction') and (parent / d).is_junction())]
        if len(parent.relative_to(destination).parts) >= 5:
            dirs[:] = []
        for name in files:
            checked += 1
            if checked > 10000:
                return False
            if name.casefold() not in {x[0] for x in wanted}:
                continue
            path = parent / name
            try:
                if not path.is_symlink():
                    found.add((name.casefold(), path.stat().st_size))
            except OSError:
                continue
        if wanted and wanted <= found:
            return True
    return False


def files_released(destination: Path, expected: list[dict]) -> bool:
    """Native clients can preallocate final size; require exclusive read access too."""
    if not present_files(destination, expected):
        return False
    if sys.platform != 'win32':
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    wanted = {(f['name'].casefold(), f['size']) for f in expected}
    checked = 0
    ready = set()
    for root, dirs, files in os.walk(destination, followlinks=False):
        parent = Path(root)
        dirs[:] = [d for d in dirs if not (parent / d).is_symlink()
                   and not (hasattr(Path, 'is_junction') and (parent / d).is_junction())]
        if len(parent.relative_to(destination).parts) >= 5:
            dirs[:] = []
        for name in files:
            checked += 1
            if checked > 10000:
                return False
            path = parent / name
            if path.is_symlink():
                continue
            try:
                key = (name.casefold(), path.stat().st_size)
            except OSError:
                continue
            if key not in wanted:
                continue
            handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
            if handle not in (None, ctypes.c_void_p(-1).value):
                kernel.CloseHandle(handle)
                ready.add(key)
    return wanted <= ready


def validate_job(body: dict) -> dict:
    destination = validate_destination(body.get('destination'))
    ids = body.get('movies')
    known = {m['id'] for m in CATALOG}
    if not isinstance(ids, list) or not ids or len(ids) > len(CATALOG) or any(not isinstance(x, str) or x not in known for x in ids):
        raise ValueError('목록에서 영화를 한 편 이상 선택하세요.')
    result = {'destination': destination, 'movies': list(dict.fromkeys(ids))}
    for key, default, low, high in [('limit', 5, 1, 20), ('budget', 25, 1, 200), ('timeout', 30, 5, 180)]:
        value = body.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key} 값은 {low}~{high} 범위의 정수여야 합니다.')
        result[key] = value
    if body.get('mode') not in ('scan', 'run'):
        raise ValueError('지원하지 않는 실행 모드입니다.')
    result['mode'] = body['mode']
    if result['mode'] == 'run' and body.get('folder_confirmed') is not True:
        raise ValueError('전용 다운로더의 실제 저장경로를 확인하고 체크하세요.')
    return result


class Controller:
    def __init__(self, data: Path = DATA):
        self.data = data
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.shutdown = threading.Event()
        self.commands = queue.Queue(maxsize=1)
        self.logs = deque(maxlen=250)
        self.results = []
        self.busy = False
        self.phase = '준비'
        self.browser = None
        self.history = {}
        self.history_error = False
        path = self.data / 'history.json'
        if path.exists():
            try:
                self.history = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(self.history, dict) or any(not isinstance(v, dict) for v in self.history.values()):
                    raise ValueError('Invalid history')
            except (OSError, ValueError):
                self.history_error = True
                self.log('이력 파일을 읽지 못했습니다. 중복 요청을 막기 위해 자동 다운로드를 차단합니다. data/history.json을 보존한 채 점검하세요.')
        self.thread = threading.Thread(target=self._worker, name='browser-owner', daemon=True)
        self.thread.start()

    def log(self, text):
        with self.lock:
            self.logs.append({'time': time.strftime('%H:%M:%S'), 'message': str(text)})

    def state(self):
        with self.lock:
            return {'busy': self.busy, 'phase': self.phase, 'logs': list(self.logs),
                    'results': json.loads(json.dumps(self.results)),
                    'history': json.loads(json.dumps(self.history)), 'catalog': CATALOG}

    def enqueue(self, command, body):
        with self.lock:
            if self.busy:
                raise ValueError('이미 작업 중입니다. 먼저 대기열을 중지하고 기다려 주세요.')
            if command == 'job':
                body = validate_job(body)
                if body['mode'] == 'run' and self.history_error:
                    raise ValueError('이력 파일이 손상되어 자동 요청이 차단되었습니다.')
            elif command == 'folder':
                body = {'destination': validate_destination(body.get('destination'))}
            elif command != 'browser':
                raise ValueError('지원하지 않는 명령입니다.')
            self.stop.clear()
            self.busy = True
            self.phase = '시작 중'
            self.commands.put_nowait((command, body))

    def record(self, movie: dict, **values):
        with self.lock:
            entry = self.history.setdefault(movie['id'], {'title': movie['title'], 'year': movie['year']})
            entry.update(values, updated=time.strftime('%Y-%m-%d %H:%M:%S'))
            atomic_json(self.data / 'history.json', self.history)

    def forget(self, movie_id: str, confirmed: bool):
        with self.lock:
            if self.busy or confirmed is not True or movie_id not in self.history:
                raise ValueError('작업이 없는 상태에서 해당 항목의 수동 확인이 필요합니다.')
            # The user, not automation, decides whether an uncertain native queue is empty.
            self.history.pop(movie_id)
            atomic_json(self.data / 'history.json', self.history)
            self.log('사용자가 해당 영화의 중복 방지 이력을 해제했습니다.')

    def _worker(self):
        while not self.shutdown.is_set():
            try:
                command, body = self.commands.get(timeout=0.15)
            except queue.Empty:
                if self.browser and self.browser.page:
                    try:
                        self.browser.page.wait_for_timeout(100)
                    except Exception:
                        self.browser.close()
                continue
            try:
                if command == 'folder':
                    self.log(change_destination(body['destination'])['message'])
                else:
                    if not self.browser:
                        self.browser = SiteBrowser(self.data / 'edge-profile', self.log, self.stop)
                    self.browser.reset_job()
                    if command == 'browser':
                        self.browser.open()
                    elif command == 'job':
                        self.run_job(body)
            except Exception as exc:
                # Never serialize Playwright request URLs, cookies, or full stack traces.
                if isinstance(exc, (Halt, ValueError, OSError)) or exc.__class__.__name__ == 'NativeUnavailable':
                    self.log(str(exc)[:600])
                else:
                    self.log('자동화를 중지했습니다 (' + type(exc).__name__ + '). Edge가 닫혔거나 사이트 응답/구조가 달라졌을 수 있습니다. 이미 요청한 항목은 자동 재시도하지 않습니다.')
                with self.lock:
                    self.phase = '확인 필요'
            finally:
                with self.lock:
                    self.busy = False
                    if self.phase == '시작 중':
                        self.phase = '준비'
                self.commands.task_done()
        if self.browser:
            self.browser.close()

    def add_result(self, movie, **values):
        row = dict(movie=movie['title'], year=movie['year'], movie_id=movie['id'], **values)
        with self.lock:
            self.results.append(row)
        return row

    def update_result(self, row, **values):
        with self.lock:
            row.update(values)

    def run_job(self, job):
        if sys.platform != 'win32':
            raise Halt('실제 검색·다운로드 실행은 윈도우 전용입니다.')
        destination = Path(job['destination'])
        destination.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or (hasattr(Path, 'is_junction') and destination.is_junction()):
            raise Halt('저장 폴더는 링크/정션이 아닌 실제 로컬 폴더여야 합니다.')
        probe = destination / ('.pdpop_write_test_' + secrets.token_hex(8))
        try:
            with probe.open('x') as stream:
                stream.write('ok')
        finally:
            probe.unlink(missing_ok=True)
        with self.lock:
            self.results = []
            self.phase = '검색 중'
        self.browser.ensure_login()
        target_ids = set(job['movies'])
        picked = used = 0
        for movie in [m for m in CATALOG if m['id'] in target_ids]:
            if self.stop.is_set() or picked >= job['limit']:
                break
            if movie['id'] in self.history:
                self.add_result(movie, status='이력 있음', note='완료 또는 미확인 요청 이력이 있어 자동 재요청하지 않습니다.')
                continue
            self.log(f"검색: {movie['title']} ({movie['year']})")
            with self.lock:
                self.phase = movie['title'] + ' 검색'
            candidates = self.browser.search(movie)
            eligible = None
            reason = '제목·연도가 일치하는 결과 없음 (최대 2페이지 검색)'
            for candidate in candidates:
                if self.stop.is_set():
                    break
                snapshot, decision = self.browser.inspect(candidate['id'], movie)
                if decision.allowed:
                    eligible = (candidate, decision)
                    break
                reason = decision.reason
                self.browser.wait(0.8)
            if self.stop.is_set():
                break
            if not eligible:
                self.add_result(movie, status='제외', note=reason)
                continue
            candidate, decision = eligible
            row = self.add_result(movie, status='후보', note=decision.reason,
                                  board=candidate['id'], size=decision.size,
                                  files=[f['name'] for f in decision.files])
            if files_released(destination, decision.files):
                self.update_result(row, status='파일 있음', note='동일 파일명·크기의 파일이 저장 폴더에 있습니다.')
                if job['mode'] == 'run':
                    self.record(movie, status='existing', board=candidate['id'])
                continue
            if used + decision.size > job['budget'] * GIB:
                self.update_result(row, status='용량 제한', note='이번 실행의 총 용량 제한을 초과합니다.')
                continue
            if shutil.disk_usage(destination).free < decision.size + 5 * GIB:
                self.update_result(row, status='공간 부족', note='다운로드 후 최소 5GB의 여유 공간이 필요합니다.')
                raise Halt('디스크 여유 공간 부족으로 중지했습니다.')
            used += decision.size
            picked += 1
            if job['mode'] == 'scan':
                self.update_result(row, status='검사 통과', note='현재 화면 기준 정액권 적용. 다운로드 요청은 보내지 않았습니다.')
                continue
            if self.stop.is_set():
                break
            # Crash-safe, at-most-once journal. An uncertain send is never retried.
            self.record(movie, status='uncertain', board=candidate['id'], destination=str(destination),
                        files=decision.files, note='실행 직전 기록. 전송 여부가 확정되지 않았습니다.')
            self.update_result(row, status='요청 중')
            try:
                self.browser.submit(candidate['id'], movie, decision)
                self.record(movie, status='requested', note='공식 요청 전송. 파일 완성 여부는 별도 확인 중입니다.')
                self.update_result(row, status='파일 대기', note='공식 다운로더에 요청했습니다. 저장 파일 크기와 잠금 해제를 확인합니다.')
                self.log(movie['title'] + ': 요청 전송. 다음 영화는 이 파일 확인 후 진행합니다.')
                with self.lock:
                    self.phase = movie['title'] + ' 파일 대기'
                deadline = time.monotonic() + job['timeout'] * 60
                stable = 0
                while time.monotonic() < deadline and not self.stop.is_set():
                    if files_released(destination, decision.files):
                        stable += 1
                    else:
                        stable = 0
                    if stable >= 3:
                        self.record(movie, status='file_checked', note='파일명·예상 크기·잠금 해제 확인. 영상 무결성 검사는 하지 않았습니다.')
                        self.update_result(row, status='파일 확인', note='파일명·예상 크기·잠금 해제 확인 (영상 무결성 미검증)')
                        self.log(movie['title'] + ': 저장 파일 확인')
                        break
                    self.browser.wait(3)
                else:
                    raise Halt('중지 또는 파일 확인 시간 초과. 전용 다운로더 상태와 저장경로를 확인하세요. 요청 이력을 보존하며 다음 영화는 진행하지 않습니다.')
            except Exception:
                self.update_result(row, status='확인 필요', note='전송 여부가 불확실할 수 있어 자동 재요청하지 않습니다. 전용 다운로더를 확인하세요.')
                raise
            with self.lock:
                self.phase = '다음 영화 검색'
        with self.lock:
            self.phase = '사용자/안전 중지' if self.stop.is_set() else ('검사 끝' if job['mode'] == 'scan' else '작업 끝')
        self.log(self.phase + '. 이미 전송 중인 파일은 전용 다운로더에서 별도로 중지할 수 있습니다.')


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = 'LocalMovieAssistant'

    def log_message(self, *_):
        pass

    def send_payload(self, code, data, mime='application/json; charset=utf-8'):
        payload = json.dumps(data, ensure_ascii=False).encode() if not isinstance(data, bytes) else data
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', self.server.csp)
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        expected_host = '127.0.0.1:' + str(self.server.server_port)
        if self.headers.get('Host') != expected_host:
            return False
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + expected_host:
            return False
        supplied = self.headers.get('Authorization', '')
        return secrets.compare_digest(supplied, 'Bearer ' + self.server.token)

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def do_GET(self):
        path = urlsplit(self.path).path
        if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
            return self.send_payload(403, {'error': 'Forbidden host'})
        if path == '/':
            return self.send_payload(200, self.server.html, 'text/html; charset=utf-8')
        if not self.authorized():
            return self.send_payload(403, {'error': 'launch.py로 실행해 자동으로 열린 제어판에서만 사용할 수 있습니다.'})
        if path == '/api/state':
            return self.send_payload(200, self.server.controller.state())
        self.send_payload(404, {'error': 'Not found'})

    def do_POST(self):
        if not self.authorized():
            return self.send_payload(403, {'error': '권한 없음'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send_payload(415, {'error': 'JSON required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > 16384:
                raise ValueError('요청 크기가 잘못되었습니다.')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('잘못된 요청입니다.')
            path = urlsplit(self.path).path
            controller = self.server.controller
            if path == '/api/stop':
                controller.stop.set()
                controller.log('대기열 중지 요청. 전용 다운로더의 진행 중 전송은 별도로 중지하세요.')
            elif path == '/api/forget':
                controller.forget(body.get('movie_id'), body.get('confirmed'))
            elif path in ('/api/browser', '/api/folder', '/api/job'):
                controller.enqueue(path.split('/')[-1], body)
            elif path == '/api/exit':
                controller.stop.set()
                controller.shutdown.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                return self.send_payload(404, {'error': 'Not found'})
            self.send_payload(200, {'ok': True})
        except (TypeError, json.JSONDecodeError):
            self.send_payload(400, {'error': '올바른 JSON 요청이 필요합니다.'})
        except ValueError as exc:
            self.send_payload(400, {'error': str(exc)[:400]})
        except OSError:
            self.send_payload(500, {'error': '로컬 파일을 저장하지 못했습니다. 폴더 권한과 디스크 공간을 확인하세요.'})


def main():
    instance = acquire_instance_lock(DATA)
    html = (ROOT / 'index.html').read_bytes()
    hashes = []
    for script in re.findall(rb'<script>(.*?)</script>', html, re.S):
        hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(script).digest()).decode() + "'")
    controller = Controller()
    server = LocalServer(('127.0.0.1', 0), Handler)
    server.controller = controller
    server.token = secrets.token_urlsafe(32)
    server.html = html
    server.csp = "default-src 'self'; script-src " + ' '.join(hashes) + "; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; connect-src 'self'"
    # Fragment is never sent in HTTP requests; it authorizes only this local UI.
    url = f'http://127.0.0.1:{server.server_port}/#' + server.token
    print('Local control panel is opening. Keep this window open; Ctrl+C stops the assistant.')
    if not webbrowser.open(url):
        print('Open this local-only address in Edge: ' + url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop.set()
        controller.shutdown.set()
        controller.thread.join(timeout=8)
        server.server_close()
        instance.close()


if __name__ == '__main__':
    main()

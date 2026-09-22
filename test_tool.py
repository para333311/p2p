"""Offline tests: python -m unittest -v test_tool.
Browser fixtures additionally run when RUN_BROWSER_TESTS=1 and Playwright is installed.
No fixture test sends a request to pdpop or starts a listening HTTP service.
"""
from __future__ import annotations

import copy
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode
from unittest.mock import Mock, patch
import subprocess

from launch import pip_arguments, prepare_runtime, validate_runtime

from app import Controller, Handler, acquire_instance_lock, atomic_json, present_files, validate_job
from browser import Halt, SiteBrowser, SNAPSHOT_JS, authorized_request, is_purchase_url
from policy import CATALOG, GIB, matches_movie, safe_relative, select_files, validate_destination

ROOT = Path(__file__).resolve().parent
TEST_DIR = ROOT / '.artifacts'
TEST_DIR.mkdir(exist_ok=True)
MOVIE = next(m for m in CATALOG if m['title'] == '사랑과 영혼')


def file_row(name='Ghost.1990.mkv', id='11', size=100):
    return dict(name=name, id=id, size=size, cine='0', disabled=False, folder='Ghost/', checked=True)


def snapshot():
    return dict(logged_in=True, fixed=1, cine=0, category='F_02', admin='', code='F_02_13',
                title='사랑과 영혼 Ghost.1990.1080p', files=[file_row(), file_row('Ghost.1990.smi', '12', 10), file_row('screen.jpg', '13', 5), file_row('setup.exe', '14', 5)])


def permit():
    return dict(board='123', code='F_02_13', files=['11', '12'], folders=[''])


def request_url(**overrides):
    fields = dict(szMode='file_download_new', szNo='123', szCode='F_02_13', szdownMode='normal',
                  szDown='0', szFile='11*12', szFolder='', szCoupon='', my_multi_port='45674', my_multi_time='example', hash='0.1')
    fields.update(overrides)
    return 'https://cgi.pdpop.com/download/?' + urlencode(fields)


class LauncherTests(unittest.TestCase):
    def test_supported_runtime_and_rejections(self):
        valid = dict(system='win32', machine='AMD64', version=(3, 13, 12), bits=64,
                     implementation='CPython', free_threaded=False)
        validate_runtime(**valid)
        for values in [dict(system='linux'), dict(machine='ARM64'), dict(bits=32),
                       dict(version=(3, 14, 0)), dict(version=(3, 13, 11)),
                       dict(implementation='PyPy'), dict(free_threaded=True)]:
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                validate_runtime(**{**valid, **values})

    def test_installer_uses_hash_pinning_and_binary_wheels(self):
        args = pip_arguments(Path('python.exe'), Path('requirements.txt'))
        self.assertEqual(args[:4], ['python.exe', '-m', 'pip', '--isolated'])
        for required in ['--require-hashes', '--only-binary=:all:', 'https://pypi.org/simple']:
            self.assertIn(required, args)
        self.assertNotIn('powershell', ' '.join(args).lower())

    def test_source_distribution_does_not_include_shell_launchers(self):
        self.assertFalse((ROOT / 'START.cmd').exists())
        self.assertFalse((ROOT / 'setup.ps1').exists())
        self.assertTrue((ROOT / 'launch.py').is_file())

    def root_and_builder(self, tmp):
        root = Path(tmp)
        (root / 'requirements.txt').write_text('example==1 --hash=sha256:abc')
        def create(env):
            (env / 'Scripts').mkdir(parents=True)
            (env / 'Scripts' / 'python.exe').write_bytes(b'fixture-not-executable')
        return root, create

    def test_setup_success_and_cached_driver_recheck(self):
        with tempfile.TemporaryDirectory(dir=TEST_DIR) as tmp:
            root, create = self.root_and_builder(tmp)
            with patch('launch.venv.EnvBuilder') as builder, patch('launch.subprocess.run') as run:
                builder.return_value.create.side_effect = create
                python = prepare_runtime(root)
                self.assertTrue(python.is_file())
                self.assertEqual(run.call_count, 2)
                self.assertTrue(all(call.kwargs['shell'] is False for call in run.call_args_list))
                self.assertTrue((python.parent.parent / 'source-ready.txt').is_file())
                builder.reset_mock(); run.reset_mock()
                prepare_runtime(root)
                builder.assert_not_called()
                self.assertEqual(run.call_count, 1)
                self.assertIn('sync_playwright', run.call_args.args[0][-1])

    def test_component_block_stops_without_ready_marker_or_retry(self):
        with tempfile.TemporaryDirectory(dir=TEST_DIR) as tmp:
            root, create = self.root_and_builder(tmp)
            with patch('launch.venv.EnvBuilder') as builder, patch('launch.subprocess.run') as run:
                builder.return_value.create.side_effect = create
                run.side_effect = [None, PermissionError('Blocked component')]
                with self.assertRaisesRegex(RuntimeError, 'keep protection enabled'):
                    prepare_runtime(root)
                self.assertEqual(run.call_count, 2)
                self.assertFalse((root / '.runtime/source-env/source-ready.txt').exists())

    def test_install_failure_does_not_launch_driver(self):
        with tempfile.TemporaryDirectory(dir=TEST_DIR) as tmp:
            root, create = self.root_and_builder(tmp)
            with patch('launch.venv.EnvBuilder') as builder, patch('launch.subprocess.run') as run:
                builder.return_value.create.side_effect = create
                run.side_effect = subprocess.CalledProcessError(1, ['pip'])
                with self.assertRaises(subprocess.CalledProcessError):
                    prepare_runtime(root)
                self.assertEqual(run.call_count, 1)
                self.assertFalse((root / '.runtime/source-env/source-ready.txt').exists())


class PolicyTests(unittest.TestCase):
    def test_catalog_is_unique_and_decade_limited(self):
        self.assertEqual(len(CATALOG), 60)
        self.assertEqual(len({m['id'] for m in CATALOG}), len(CATALOG))
        self.assertTrue(all(1990 <= m['year'] <= 1999 for m in CATALOG))
        self.assertNotIn('죽은시인의사회', {m['title'] for m in CATALOG})

    def test_catalog_titles_match_their_year(self):
        for m in CATALOG:
            with self.subTest(movie=m['title']):
                self.assertTrue(matches_movie(f"{m['title']} ({m['year']}) 1080p.BluRay", m))

    def test_title_and_year_are_both_required(self):
        for title in ['사랑과 영혼', '다른 영화 1990', '사랑과 영혼 2026', '1990년대 사랑과 영혼', '사랑과 영혼 19901', '사랑과 영혼 1990 2020', '사랑과 영혼 전집 1990', '사랑과 영혼 2 1990']:
            with self.subTest(title=title):
                self.assertFalse(matches_movie(title, MOVIE))
        self.assertTrue(matches_movie('사랑과영혼_Ghost (1990) BluRay.1080p', MOVIE))

    def test_media_only(self):
        d = select_files(snapshot())
        self.assertTrue(d.allowed)
        self.assertEqual([f['id'] for f in d.files], ['11', '12'])
        self.assertEqual(d.size, 110)

    def test_fail_closed_account_and_category(self):
        for key, values in {'logged_in': [False, None, 1, 'true'], 'fixed': [0, None, '1', True, 2], 'cine': [1, None, '0', False], 'category': ['F_03', '', None]}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    s = snapshot(); s[key] = value
                    self.assertFalse(select_files(s).allowed)

    def test_invalid_file_metadata(self):
        for key, values in {'cine': [None, '1', 0], 'disabled': [True, None], 'id': ['', 'x', '1&2', 1], 'size': [-1, 0, None, '100', True], 'name': ['../a.mkv', 'NUL.mkv', 'movie.exe:video.mkv'], 'folder': ['../escape/', 'C:\\outside\\']}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    s = snapshot(); s['files'][0][key] = value
                    self.assertFalse(select_files(s).allowed)

    def test_no_video_multiple_video_duplicate_ids_and_limits(self):
        s = snapshot(); s['files'] = s['files'][1:]
        self.assertFalse(select_files(s).allowed)
        s = snapshot(); s['files'].append(file_row('part2.mkv', '99'))
        self.assertFalse(select_files(s).allowed)
        s = snapshot(); s['files'][1]['id'] = '11'
        self.assertFalse(select_files(s).allowed)
        s = snapshot(); s['files'][0]['size'] = 13 * GIB
        self.assertFalse(select_files(s).allowed)

    def test_korean_destination(self):
        self.assertEqual(validate_destination('E:/원본소스'), r'E:\원본소스')

    def test_windows_paths(self):
        self.assertEqual(validate_destination('E:/pdpop'), 'E:\\pdpop')
        self.assertTrue(safe_relative('영화/자막'))
        for path in ['E:\\', 'relative', '\\\\host\\share', 'E:\\x\\..\\y', 'E:\\CON', 'E:\\test:stream', 'E:\\bad?name', '', None]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_destination(path)

    def test_job_validation(self):
        body = dict(destination='E:\\pdpop', movies=[MOVIE['id']], mode='run', folder_confirmed=True)
        self.assertEqual(validate_job(body)['limit'], 5)
        for patch in [dict(folder_confirmed=False), dict(movies=[]), dict(movies=['unknown']), dict(limit=0), dict(limit=True), dict(limit=21), dict(timeout=0), dict(budget=500), dict(mode='purchase')]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_job({**body, **patch})
        self.assertEqual(validate_job({**body, 'mode': 'scan', 'folder_confirmed': False})['mode'], 'scan')


class GateTests(unittest.TestCase):
    def test_one_exact_request_shape(self):
        self.assertTrue(authorized_request(request_url(), permit()))
        for patch in [dict(szNo='999'), dict(szCode='F_12'), dict(szMode='buy'), dict(szCoupon='coupon'), dict(szFile='11*12*13'), dict(szFile='11*11'), dict(szFolder='Ghost/'), dict(my_multi_port='9999'), dict(extra_charge='1')]:
            with self.subTest(patch=patch):
                self.assertFalse(authorized_request(request_url(**patch), permit()))
        for url in [request_url().replace('https:', 'http:'), request_url().replace('cgi.pdpop.com', 'evil.test'), request_url() + '&szNo=123']:
            self.assertFalse(authorized_request(url, permit()))
        self.assertFalse(authorized_request(request_url(), None))

    def test_url_classification(self):
        self.assertTrue(is_purchase_url(request_url()))
        self.assertTrue(is_purchase_url('https://m.pdpop.com/doc/board/board_view.enroll.php'))
        self.assertFalse(is_purchase_url('https://new.pdpop.com/js/view/download.js'))
        self.assertFalse(is_purchase_url('https://new.pdpop.com/api/login'))

    def test_permission_consumed_before_continue(self):
        site = SiteBrowser(TEST_DIR, lambda _: None, threading.Event())
        site.permit = permit()
        route = Mock(request=SimpleNamespace(url=request_url(), method='GET'))
        route.continue_.side_effect = lambda: self.assertIsNone(site.permit)
        site._route(route)
        self.assertTrue(site.sent)
        route.continue_.assert_called_once()
        second = Mock(request=route.request)
        site._route(second)
        second.abort.assert_called_once()
        second.continue_.assert_not_called()

    def test_stopped_and_unpermitted_requests_blocked(self):
        for stopped in [False, True]:
            site = SiteBrowser(TEST_DIR, lambda _: None, threading.Event())
            if stopped:
                site.permit = permit(); site.stop.set()
            route = Mock(request=SimpleNamespace(url=request_url(), method='GET'))
            site._route(route)
            route.abort.assert_called_once()
            self.assertFalse(site.sent)

    def test_dialog_never_accepted(self):
        site = SiteBrowser(TEST_DIR, lambda _: None, threading.Event())
        dialog = Mock(type='confirm')
        site._dialog(dialog)
        dialog.dismiss.assert_called_once()
        dialog.accept.assert_not_called()
        self.assertTrue(site.stop.is_set())


class LocalStateTests(unittest.TestCase):
    def test_atomic_history_and_no_file_name_only_completion(self):
        with tempfile.TemporaryDirectory(dir=TEST_DIR) as tmp:
            root = Path(tmp)
            atomic_json(root / 'history.json', {'a': '한글'})
            self.assertEqual(json.loads((root / 'history.json').read_text()), {'a': '한글'})
            self.assertFalse((root / 'history.tmp').exists())
            wanted = [file_row(size=10)]
            (root / 'Ghost.1990.mkv').write_bytes(b'x' * 9)
            self.assertFalse(present_files(root, wanted))
            (root / 'Ghost.1990.mkv').write_bytes(b'x' * 10)
            self.assertTrue(present_files(root, wanted))

    def test_symlink_files_do_not_count(self):
        with tempfile.TemporaryDirectory(dir=TEST_DIR) as tmp:
            root = Path(tmp); (root / 'original').write_bytes(b'abc')
            try:
                (root / 'Ghost.1990.mkv').symlink_to(root / 'original')
            except OSError:
                self.skipTest('Symlinks unavailable')
            self.assertFalse(present_files(root, [file_row(size=3)]))

    def test_instance_lock_rejects_second_owner(self):
        with tempfile.TemporaryDirectory(dir=TEST_DIR) as tmp:
            one = acquire_instance_lock(Path(tmp))
            try:
                with self.assertRaises(RuntimeError):
                    acquire_instance_lock(Path(tmp))
            finally:
                one.close()
            two = acquire_instance_lock(Path(tmp)); two.close()

    def test_history_survives_restart(self):
        with tempfile.TemporaryDirectory(dir=TEST_DIR) as tmp:
            root = Path(tmp); controller = Controller(root)
            try:
                controller.record(MOVIE, status='uncertain', board='123')
                self.assertEqual(json.loads((root / 'history.json').read_text())[MOVIE['id']]['status'], 'uncertain')
                with self.assertRaises(ValueError):
                    controller.forget(MOVIE['id'], False)
            finally:
                controller.shutdown.set(); controller.thread.join(2)
            second = Controller(root)
            try:
                self.assertIn(MOVIE['id'], second.history)
            finally:
                second.shutdown.set(); second.thread.join(2)

    def test_corrupt_history_blocks_downloads(self):
        with tempfile.TemporaryDirectory(dir=TEST_DIR) as tmp:
            root = Path(tmp); (root / 'history.json').write_text('{broken')
            c = Controller(root)
            try:
                self.assertTrue(c.history_error)
                with self.assertRaises(ValueError):
                    c.enqueue('job', dict(destination='E:\\pdpop', movies=[MOVIE['id']], mode='run', folder_confirmed=True))
            finally:
                c.shutdown.set(); c.thread.join(2)

    def handler(self, **headers):
        h = Handler.__new__(Handler)
        h.headers = Message()
        for k, v in {'Host': '127.0.0.1:8787', 'Authorization': 'Bearer test-token', **headers}.items():
            h.headers[k] = v
        h.server = SimpleNamespace(server_port=8787, token='test-token')
        return h

    def test_loopback_token_origin_and_host(self):
        self.assertTrue(self.handler().authorized())
        for header in [{'Host': 'evil.test'}, {'Origin': 'https://evil.test'}, {'Authorization': 'Bearer wrong'}, {'Authorization': ''}]:
            self.assertFalse(self.handler(**header).authorized())

    def test_post_rejects_bad_json_and_oversized_body(self):
        for body, length in [(b'not-json', '8'), (b'{}', '999999'), (b'[]', '2')]:
            h = self.handler(**{'Content-Type': 'application/json', 'Content-Length': length})
            h.rfile = io.BytesIO(body); h.send_payload = Mock(); h.path = '/api/job'
            h.do_POST(); self.assertEqual(h.send_payload.call_args.args[0], 400)


FIXTURE = """<!doctype html><meta charset="utf-8"><title>사랑과 영혼 Ghost.1990.1080p | 피디팝</title>
<input type="checkbox" name="folder" value="Ghost/" checked><input id="checkAll" type="checkbox" checked>
<input type="checkbox" name="folderBottom" value="Ghost/" checked><input id="checkAllBottom" type="checkbox" checked>
{rows}
<button onclick="downloadFile('firstDownload');">다운로드</button>
<script>
let isMemberFixedDownload=1, isBoardCine=0, cate1='F_02',code='F_02_13',isAdmin='',selectCineFile=0;
const getCookie=()=> 'fixture-user';
function selectFileCalculate() {{selectCineFile=[...document.querySelectorAll('input[name="fileList"]:checked')].filter(e=>e.dataset.iscine==='1').length;}}
function downloadFile() {{
 const q=new URLSearchParams({{szMode:'file_download_new',szNo:'123',szCode:code,szdownMode:'normal',szDown:'0',szCoupon:'',my_multi_port:'45674',hash:'0.1',my_multi_time:'example',szFile:[...document.querySelectorAll('input[name="fileList"]:checked')].map(e=>e.dataset.fileno).join('*'),szFolder:[...document.querySelectorAll('input[name="folder"]:checked')].map(e=>e.value).join('*')}});
 fetch('https://cgi.pdpop.com/download/?'+q).catch(()=>{{}});
}}
</script>"""


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == '1', 'Set RUN_BROWSER_TESTS=1 for offline browser fixtures')
class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.p = sync_playwright().start()
        cls.browser = cls.p.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.p.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.tool = SiteBrowser(TEST_DIR, lambda _: None, threading.Event())
        self.tool.context = self.context
        self.tool.page = self.context.new_page()
        self.tool._attach_page(self.tool.page)
        self.sent = []
        rows = ''.join(f'<ul><li><input type="checkbox" name="fileList" data-fileno="{f["id"]}" data-folder="Ghost/" data-size="{f["size"]}" data-iscine="0" checked></li><li><p class="fileRealName">{f["name"]}</p></li></ul>' for f in snapshot()['files'])
        html = FIXTURE.format(rows=rows)
        def route_handler(route):
            if is_purchase_url(route.request.url):
                def fulfilled():
                    self.sent.append(route.request.url)
                    route.fulfill(status=200, body='ok', headers={'Access-Control-Allow-Origin': '*'})
                # Guard is real; network continuation is ALWAYS replaced with a fixture.
                self.tool._route(SimpleNamespace(request=route.request, abort=route.abort, continue_=fulfilled))
            else:
                route.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)
        self.context.route('**/*', route_handler)
        self.tool.page.goto('https://new.pdpop.com/view/123')

    def tearDown(self):
        self.context.close()

    def test_real_dom_selection_and_one_shot_handoff(self):
        snap = self.tool.page.evaluate(SNAPSHOT_JS)
        decision = select_files(snap)
        self.assertTrue(decision.allowed)
        self.tool.submit('123', MOVIE, decision)
        self.assertTrue(self.tool.sent)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(authorized_request(self.sent[0], permit()))
        self.assertEqual(self.tool.page.locator('input[name="folder"]:checked').count(), 0)
        self.assertEqual(self.tool.page.locator('input[name="fileList"]:checked').count(), 2)

    def test_subscription_change_before_click_prevents_request(self):
        decision = select_files(self.tool.page.evaluate(SNAPSHOT_JS))
        self.tool.page.evaluate('isMemberFixedDownload=0')
        with self.assertRaises(Halt):
            self.tool.submit('123', MOVIE, decision)
        self.assertEqual(self.sent, [])

    def test_real_purchase_confirm_is_dismissed(self):
        accepted = self.tool.page.evaluate('confirm("포인트로 구매하시겠습니까?")')
        self.assertFalse(accepted)
        self.assertTrue(self.tool.stop.is_set())

    def test_dashboard_without_token_shows_source_start_instructions(self):
        self.context.unroute_all()
        html = (ROOT / 'index.html').read_text()
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=html))
        page = self.tool.page
        page.goto('http://127.0.0.1:8787/')
        page.locator('#installHelp').wait_for(state='visible')
        self.assertIn('py -3.13 launch.py', page.locator('#installHelp').inner_text())
        self.assertTrue(page.locator('#run').is_disabled())

    def test_dashboard_escaping_selection_and_folder_gate(self):
        self.context.unroute_all()
        html = (ROOT / 'index.html').read_text()
        state = dict(catalog=CATALOG, busy=False, phase='검증용 상태', logs=[], history={}, results=[dict(movie='<img src=x onerror=alert(1)>', year=1990, status='후보', note='<script>bad</script>')])
        calls = []
        def handler(route):
            if '/api/' in route.request.url:
                if route.request.method == 'POST':
                    calls.append(route.request.post_data_json)
                    route.fulfill(json={'ok': True})
                else:
                    route.fulfill(json=state)
            else:
                route.fulfill(content_type='text/html; charset=utf-8', body=html)
        self.context.route('**/*', handler)
        page = self.tool.page
        page.goto('http://127.0.0.1:8787/#test-token')
        page.wait_for_function('document.querySelectorAll(".movie").length === 60')
        self.assertEqual(page.locator('#results img').count(), 0)
        self.assertIn('<img', page.locator('#results').inner_text())
        page.locator('#run').click()
        self.assertIn('저장 폴더', page.locator('#error').inner_text())
        self.assertEqual(calls, [])
        page.locator('#folderConfirmed').check()
        page.locator('#run').click()
        page.wait_for_timeout(100)
        self.assertEqual(calls[-1]['mode'], 'run')
        self.assertEqual(calls[-1]['destination'], 'E:\\원본소스')
        self.assertEqual(validate_job(calls[-1])['destination'], 'E:\\원본소스')
        page.locator('#clearSelection').click()
        self.assertIn('0편', page.locator('#selectedCount').inner_text())
        page.locator('#yearFilter').select_option('1999')
        page.locator('#selectVisible').click()
        self.assertIn('7편', page.locator('#selectedCount').inner_text())
        page.screenshot(path=str(TEST_DIR / 'dashboard_fixture.png'), full_page=True)


if __name__ == '__main__':
    unittest.main()

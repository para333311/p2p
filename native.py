"""Conservative Windows accessibility integration; never use screen coordinates."""
from __future__ import annotations

import ctypes
from pathlib import Path
import re
import sys
import time

from policy import validate_destination


class NativeUnavailable(RuntimeError):
    pass


def process_image(pid: int) -> str:
    if sys.platform != 'win32':
        return ''
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ''
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        return buf.value if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ''
    finally:
        kernel.CloseHandle(handle)


def change_destination(destination: str) -> dict:
    """Try a recognizable native folder dialog. User confirms the resulting setting."""
    if sys.platform != 'win32':
        raise NativeUnavailable('저장 경로 자동 설정은 윈도우에서만 사용할 수 있습니다.')
    destination = validate_destination(destination)
    Path(destination).mkdir(parents=True, exist_ok=True)
    from pywinauto import Desktop
    windows = Desktop(backend='win32').windows(visible_only=True)
    candidates = []
    for win in windows:
        image = Path(process_image(win.process_id())).name.lower()
        if re.search(r'(pdpop|pdp.*down|pdown)', image) and not re.search(r'(updat|setup|install|uninst)', image):
            candidates.append(win)
    if not candidates:
        raise NativeUnavailable('실행 중인 피디팝 다운로더를 인식하지 못했습니다. 다운로더를 열고 다시 시도하거나 저장경로변경에서 직접 지정하세요.')
    target = None
    for win in candidates:
        ui = Desktop(backend='uia').window(handle=win.handle).wrapper_object()
        buttons = [b for b in ui.descendants(control_type='Button')
                   if re.sub(r'\s', '', b.window_text()) == '저장경로변경']
        if len(buttons) == 1:
            if target:
                raise NativeUnavailable('다운로더 창이 여러 개입니다. 하나만 남겨 주세요.')
            target = (ui, buttons[0])
    if not target:
        raise NativeUnavailable('이 다운로더는 저장경로변경 버튼을 접근성 API에 공개하지 않습니다. 좌표를 추측하지 않습니다. 다운로더에서 폴더를 한 번 직접 지정한 뒤 확인 체크를 해 주세요.')
    ui, button = target
    pid = ui.process_id()
    button.invoke()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        for dialog in Desktop(backend='uia').windows(process=pid, visible_only=True):
            choices = [b for b in dialog.descendants(control_type='Button')
                       if re.sub(r'[\s&]', '', b.window_text()) in ('폴더선택', 'SelectFolder')]
            edits = [e for e in dialog.descendants(control_type='Edit') if e.is_visible() and e.is_enabled()]
            preferred = [e for e in edits if e.element_info.automation_id == '1152'
                         or re.sub(r'[\s&:]', '', e.window_text()) in ('폴더', 'Folder')]
            edit = preferred[0] if len(preferred) == 1 else (edits[0] if len(edits) == 1 else None)
            if len(choices) == 1 and edit:
                edit.set_edit_text(destination)
                if edit.get_value().casefold().rstrip('\\') != destination.casefold().rstrip('\\'):
                    raise NativeUnavailable('폴더 경로 입력값이 일치하지 않아 중지했습니다.')
                choices[0].invoke()
                return {'requested': True, 'message': '폴더 선택 요청을 보냈습니다. 다운로더의 실제 저장경로가 ' + destination + '인지 확인하고 체크해 주세요. 기존 전송의 경로까지 변경되었다고 보장하지 않습니다.'}
        time.sleep(0.25)
    raise NativeUnavailable('표준 폴더 선택창을 식별하지 못했습니다. 열린 창에서 폴더를 직접 지정해 주세요. 임의 버튼은 누르지 않았습니다.')

"""Run source with official CPython: py -3.13 launch.py.

This is not a signed application and does not guarantee Smart App Control
compatibility. No security setting, file trust marker or policy is changed.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig
import venv

ROOT = Path(__file__).resolve().parent
INSTALLER = 'https://www.python.org/ftp/python/3.13.12/python-3.13.12-amd64.exe'
CHECK_IMPORTS = (
    'import pywinauto, win32api\n'
    'from playwright.sync_api import sync_playwright\n'
    'with sync_playwright() as p:\n'
    '    assert p.chromium is not None\n'
    'print("Dependencies and browser driver started successfully.")\n'
)


def validate_runtime(system: str, machine: str, version: tuple, bits: int,
                     implementation: str, free_threaded: bool):
    if system != 'win32' or machine.upper() not in ('AMD64', 'X86_64') or bits != 64:
        raise RuntimeError('Windows x64 is required. This version is not tested on ARM or 32-bit Windows.')
    if implementation != 'CPython' or version[:2] != (3, 13) or version < (3, 13, 12) or free_threaded:
        raise RuntimeError('Install standard Python 3.13.12 or newer 3.13.x (64-bit), then run: py -3.13 launch.py\n' + INSTALLER)


def pip_arguments(python: Path, requirements: Path) -> list[str]:
    return [str(python), '-m', 'pip', '--isolated', 'install',
            '--index-url', 'https://pypi.org/simple', '--disable-pip-version-check',
            '--no-cache-dir', '--only-binary=:all:', '--require-hashes',
            '-r', str(requirements)]


def ready_signature(requirements: Path) -> str:
    content = requirements.read_bytes()
    runtime = '|'.join((sys.executable, sys.version, platform.machine())).encode()
    return hashlib.sha256(content + b'\0' + runtime).hexdigest()


def prepare_runtime(root: Path) -> Path:
    env_dir = root / '.runtime' / 'source-env'
    python = env_dir / 'Scripts' / 'python.exe'
    marker = env_dir / 'source-ready.txt'
    requirements = root / 'requirements.txt'
    signature = ready_signature(requirements)
    try:
        ready = python.is_file() and marker.read_text(encoding='ascii').strip() == signature
    except FileNotFoundError:
        ready = False
    if not ready:
        print('First run: creating a private virtual environment. Keep this window open.')
        print('Packages are downloaded from PyPI and checked against pinned SHA256 hashes.')
        venv.EnvBuilder(with_pip=True, symlinks=False).create(env_dir)
        marker.unlink(missing_ok=True)
        subprocess.run(pip_arguments(python, requirements), cwd=root, check=True, shell=False)
    # Check native imports and start Playwright's actual driver BEFORE the UI.
    # If Windows blocks a component, do not stamp setup complete or try an alternate path.
    try:
        subprocess.run([str(python), '-c', CHECK_IMPORTS], cwd=root, check=True,
                       shell=False, timeout=45)
    except (OSError, subprocess.SubprocessError):
        marker.unlink(missing_ok=True)
        raise RuntimeError('A required component could not start. If Windows reports a blocked file, keep protection enabled and record that filename. No bypass or retry was attempted.') from None
    if not ready:
        marker.write_text(signature + '\n', encoding='ascii')
    return python


def main() -> int:
    lock = None
    try:
        validate_runtime(sys.platform, platform.machine(), tuple(sys.version_info[:3]),
                         64 if sys.maxsize > 2 ** 32 else 32, platform.python_implementation(),
                         bool(sysconfig.get_config_var('Py_GIL_DISABLED')))
        os.chdir(ROOT)
        os.environ['PYTHONUTF8'] = '1'
        for stream in (sys.stdout, sys.stderr):
            if stream and hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='replace')
        # Shared helper imports only stdlib at module load; third-party imports are lazy.
        from app import acquire_instance_lock
        lock = acquire_instance_lock(ROOT / '.runtime' / 'source-launcher')
        python = prepare_runtime(ROOT)
        print('Opening the control panel. Login and destination remain on this PC.')
        print('Keep this terminal open. Stop from the panel or press Ctrl+C.')
        return subprocess.run([str(python), str(ROOT / 'app.py')], cwd=ROOT,
                              check=False, shell=False).returncode
    except KeyboardInterrupt:
        print('\nStopped. Transfers already sent to the official downloader may continue.')
        return 130
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print('\nSetup stopped: ' + str(exc))
        print('Do not disable Smart App Control or remove security markings to continue.')
        return 1
    finally:
        if lock:
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())

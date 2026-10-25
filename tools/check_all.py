"""Run repeatable local checks with isolated state and no model generation."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def stop(process):
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                if os.name == 'posix':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            process.wait()


@contextlib.contextmanager
def isolated_display(output, env):
    if not sys.platform.startswith('linux') or not shutil.which('dbus-run-session'):
        raise RuntimeError('GUI checks need Linux and dbus-run-session. Use --no-gui for CLI/API checks.')
    executable = shutil.which('Xvfb')
    screen = ['-screen', '0', '1600x1100x24']
    if not executable:
        executable = shutil.which('Xephyr') if env.get('DISPLAY') else None
        screen = ['-screen', '1600x1100']
    if not executable:
        raise RuntimeError('Install Xvfb, or use Xephyr from a graphical Linux session. Use --no-gui to skip GUI checks.')
    print('GUI display: ' + Path(executable).name + ' (sample data only)', flush=True)
    with (output / 'display-number.txt').open('w') as number, (output / 'display.log').open('w') as log:
        process = subprocess.Popen([executable, '-displayfd', str(number.fileno()), '-nolisten', 'tcp', '-noreset', *screen],
                                   pass_fds=(number.fileno(),), env=env, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and process.poll() is None:
                display = (output / 'display-number.txt').read_text().strip()
                if display.isdecimal():
                    yield dict(env, DISPLAY=':' + display, GDK_BACKEND='x11', GSK_RENDERER='cairo',
                               GTK_A11Y='none', GIO_USE_VFS='local', GTK_USE_PORTAL='0')
                    return
                time.sleep(.1)
            raise RuntimeError('Test display did not start. See display.log.')
        finally:
            stop(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--no-gui', action='store_true', help='Run Python/CLI/API tests and JavaScript syntax only')
    group.add_argument('--gui-only', action='store_true', help='Run native layout and startup checks only')
    parser.add_argument('--output', type=Path, help='Create this new report directory (default: a temporary folder)')
    args = parser.parse_args()
    if args.output:
        output = args.output.expanduser().resolve()
        try:
            output.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            parser.error('--output must name a new directory so previous reports are preserved')
    else:
        output = Path(tempfile.mkdtemp(prefix='coding-hub-checks-'))
    env = dict(os.environ, CODING_HUB_STATE=str(output / 'state'), PYTHONUNBUFFERED='1')
    checks = []
    print('Report: ' + str(output), flush=True)
    print('Scripted checks with sample data; no AI prompts or token usage.', flush=True)

    def check(name, command, environment=env, timeout=180):
        print('Running ' + name + '…', flush=True)
        started = time.monotonic()
        path = output / (name + '.log')
        with path.open('w') as log:
            try:
                process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, start_new_session=(os.name == 'posix'))
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    log.write('\nCheck timed out.\n')
                    code = 124
                finally:
                    stop(process)
            except OSError as error:
                log.write(str(error) + '\n')
                code = 1
        checks.append(dict(name=name, exit_code=code, seconds=round(time.monotonic() - started, 2), log=path.name))
        print(('PASS ' if code == 0 else 'FAIL ') + name + f' ({checks[-1]["seconds"]}s)', flush=True)
        if code:
            print('\n'.join(path.read_text(errors='replace').splitlines()[-12:]), flush=True)
        return code == 0

    try:
        if not args.gui_only:
            check('python-cli-api', [sys.executable, '-m', 'unittest', 'discover', '-p', 'test_*.py', '-v'])
            check('javascript-syntax', [shutil.which('node') or 'node', '--check', 'assets/app.js'])
            check('workspace-tools-syntax', [shutil.which('node') or 'node', '--check', 'assets/features.js'])
            check('browser-runner-syntax', [shutil.which('node') or 'node', '--check', 'tools/browser_check.mjs'])
        if not args.no_gui and check('gtk-dependencies', [sys.executable, '-c',
                "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk"]):
            with isolated_display(output, env) as gui_env:
                check('native-layouts', ['dbus-run-session', '--', sys.executable, 'tools/check_desktop_ui.py',
                                        '--output', str(output / 'screenshots')], gui_env)
                check('native-startup', ['dbus-run-session', '--', sys.executable, 'tools/check_startup_ui.py'], gui_env)
    except (RuntimeError, OSError, KeyboardInterrupt) as error:
        print(str(error) or 'Checks interrupted.', flush=True)
        checks.append(dict(name='runner', exit_code=1, error=str(error)))
    success = all(item['exit_code'] == 0 for item in checks)
    summary = dict(success=success, checks=checks, gui_requested=not args.no_gui,
                   screenshots=len(list((output / 'screenshots').glob('*.png'))))
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(('All requested checks passed.' if success else 'Checks failed; see the report.') + '\nReport: ' + str(output), flush=True)
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())

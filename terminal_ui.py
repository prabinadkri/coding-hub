"""Small terminal presentation helpers; no external packages or saved prompts."""
from __future__ import annotations
import os
import re
import shutil
import sys
import time

COMMANDS = ('/help', '/models', '/model', '/route', '/edit', '/paste', '/new', '/memory', '/changes', '/status', '/doctor', '/quit')


def interactive():
    return sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'


def safe_text(text):
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', str(text))
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', '', text)


def style(text, code='1;38;5;150'):
    plain = safe_text(text)
    return '\x1b[' + code + 'm' + plain + '\x1b[0m' if interactive() and 'NO_COLOR' not in os.environ else plain


def width():
    return max(20, min(100, shutil.get_terminal_size((88, 24)).columns - 4))


def banner(project, backend, model, apply):
    print('\n' + style('  ◇ Coding Hub') + style('  /  terminal workspace', '2'))
    print('  ' + safe_text(str(project)))
    print('  ' + style(backend + ' · ' + (model or 'route default') + ' · ' + ('edits enabled' if apply else 'analysis only'), '2'))
    print(style('  ' + '─' * max(1, width() - 2), '2'))
    print('  /help commands   /models choose model   /paste multiline')
    print(style('  Ctrl+C stops the active task. Your partial changes stay in place.', '2'))


def help_text():
    print('\n' + style('Conversation'))
    print('  /new              New chat in this project\n  /paste            Multiline message; finish with a single .\n  /quit             Leave the chat')
    print('\n' + style('Agent'))
    print('  /route NAME       auto, smart, antigravity, free, local, claude, openai\n  /models           List available models\n  /model ID         Choose a model; use default to reset\n  /edit on|off      Enable or disable file edits and commands')
    print('\n' + style('Project'))
    print('  /changes          Review the latest task diff\n  /memory           Shared instructions and index coverage\n  /status           Project, route, model, and edit mode\n  /doctor           Check Linux; include report in your next message')
    print(style('\n  Existing :commands work too. Use codehub quota for usage details.', '2'))


def memory_text(info):
    print('\n' + style('Project memory'))
    print(f"  {info['conversations']} chats · {info['turns']} saved turns")
    print(f"  Source index: {info['indexed_files']}/{info['eligible_files']} files" + (' · partial coverage' if not info.get('complete') else ''))
    names = ', '.join(item['name'] for item in info.get('guidance_files', []))
    print('  Instructions: ' + (safe_text(names) or 'No instruction files yet'))
    print('\n' + safe_text(info.get('requirements') or 'No pinned requirements yet. Add them from Project memory in the app or web.'))


def progress_frame(label, started, frame):
    elapsed = max(0, int(time.monotonic() - started))
    clock = f'{elapsed // 60}m {elapsed % 60:02}s' if elapsed >= 60 else f'{elapsed}s'
    text = '  ' + '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[frame % 10] + ' ' + safe_text(label) + ' · ' + clock + ' · Ctrl+C to stop'
    return '\r\x1b[2K' + style(text[:width()], '38;5;150')

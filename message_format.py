"""Small, safe message presentation shared by the desktop and terminal clients."""
from __future__ import annotations
import html
import os
import re
import shutil
import sys
import textwrap


def clean_reply(text):
    # Model reasoning belongs outside the final answer. Preserve literal code fences.
    pieces = re.split(r'(```[\s\S]*?```)', str(text))
    for i, piece in enumerate(pieces):
        if not piece.startswith('```'):
            pieces[i] = re.sub(r'<think>[\s\S]*?(?:</think>|$)', '', piece, flags=re.I)
    return ''.join(pieces).strip()


def blocks(text, with_languages=False):
    result, paragraph, code, language, fenced = [], [], [], '', False
    def flush():
        if paragraph:
            result.append(('paragraph', '\n'.join(paragraph)))
            paragraph.clear()
    for line in clean_reply(text).splitlines():
        if line.startswith('```'):
            if fenced:
                result.append(('code', (language, '\n'.join(code)) if with_languages else '\n'.join(code)))
                code.clear()
            else:
                flush()
                language = line[3:].strip()
            fenced = not fenced
        elif fenced:
            code.append(line)
        elif not line.strip():
            flush()
        elif re.match(r'^#{1,6}\s', line):
            flush(); result.append(('heading', re.sub(r'^#{1,6}\s+', '', line)))
        elif re.match(r'^\s*[-*+]\s+', line):
            flush(); result.append(('list', re.sub(r'^\s*[-*+]\s+', '• ', line)))
        elif re.match(r'^\s*\d+[.)]\s+', line):
            flush(); result.append(('list', line.strip()))
        elif line.startswith('> '):
            flush(); result.append(('quote', line[2:]))
        elif re.fullmatch(r'\s*[-*_]{3,}\s*', line):
            flush()
        else:
            paragraph.append(line)
    flush()
    if code:
        result.append(('code', (language, '\n'.join(code)) if with_languages else '\n'.join(code)))
    return result


def inline_markup(text):
    parts = re.split(r'(\*\*[^*\n]+\*\*|`[^`\n]+`)', text)
    return ''.join('<b>'+html.escape(p[2:-2])+'</b>' if p.startswith('**') and p.endswith('**')
                   else '<tt>'+html.escape(p[1:-1])+'</tt>' if p.startswith('`') and p.endswith('`')
                   else html.escape(p) for p in parts)


def terminal_reply(text):
    color = sys.stdout.isatty() and not os.environ.get('NO_COLOR')
    width = max(40, min(100, shutil.get_terminal_size((88, 24)).columns - 4))
    heading = '\x1b[1;38;5;150m' if color else ''
    reset = '\x1b[0m' if color else ''
    print('\n' + heading + 'Coding Hub' + reset + '\n')
    for kind, content in blocks(text):
        if kind == 'code':
            print('\n'.join('    '+line for line in content.splitlines()))
        else:
            plain = re.sub(r'\*\*([^*]+)\*\*|`([^`]+)`', lambda m: m.group(1) or m.group(2), content)
            if kind == 'heading':
                print(heading+plain+reset)
            else:
                for line in plain.splitlines():
                    print(textwrap.fill(line, width=width, subsequent_indent='  ' if kind=='list' else ''))
        if kind != 'list': print()

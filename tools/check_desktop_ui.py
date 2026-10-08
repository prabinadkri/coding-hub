"""Render the real GTK widgets with synthetic data and no provider requests.

Run on Linux with GTK 4, under a dedicated display/session such as:
  xvfb-run -a dbus-run-session python3 tools/check_desktop_ui.py --output /tmp/hub-ui
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop
import hub


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Gsk', '4.0')
    from gi.repository import Gtk, Gdk, Gio, GLib, Gsk

    now = time.time()
    usage = {'responses': 12, 'total_tokens': 24850, 'tokens': {'input': 14000, 'output': 2850, 'reasoning': 0, 'cache_read': 8000, 'cache_write': 0}}
    status = {'checking': False, 'programs': {'agy': True, 'opencode': True, 'ollama': True},
              'free_models': list(hub.FREE_MODELS), 'local_ready': True, 'local_model': hub.LOCAL_AGENT_MODEL,
              'gpu': {'name': 'NVIDIA GeForce GTX 1650', 'used_mb': 128, 'total_mb': 4096},
              'ram': {'available_gb': 10.9, 'total_gb': 15.3}, 'loaded': [],
              'quota': {'checked_at': now, 'groups': [
                  {'name': 'Gemini Models', 'buckets': [{'remaining_percent': 82.5, 'reset_at': now + 90000}]},
                  {'name': 'Claude and GPT models', 'buckets': [{'remaining_percent': 100, 'reset_at': now + 172000}]}]},
              'free_quota': {'checked_at': now, 'expected_reset_at': (int(now // 86400) + 1) * 86400,
                  'coverage': 'These are local response and token counts, not requests remaining. Models can share limits across devices.',
                  'models': [{'id': mid, 'name': name, 'status': 'not_reported', 'usage': usage, 'observation': None} for mid, name in zip(hub.FREE_MODELS, ['Space Bunny', 'LongCat 2.5 Preview', 'Big Pickle'])],
                  'local': {'usage': usage}}}
    project = '/workspace/studio'
    task = {'id': 'preview-task', 'conversation': 'preview-chat', 'status': 'completed', 'prompt': 'Improve the project dashboard layout',
            'project': project, 'backend': 'smart', 'created_at': now - 300, 'started_at': now - 290, 'ended_at': now - 230,
            'output': 'Updated the dashboard spacing and navigation.\nValidation passed.'}
    conversation = {'id': 'preview-chat', 'project': project, 'goal': task['prompt'], 'total_turns': 1,
                    'turns': [{'rowid': 1, 'request': task['prompt'], 'result': 'The dashboard now has clear navigation, grouped metrics, and a focused workspace. Layout checks passed.', 'status': 'completed'}]}
    requests = []

    def api(path, body=None):
        requests.append((path, body))
        if path == 'status': return status
        if path == 'tasks': return {'tasks': [task]}
        if path.startswith('tasks/'): return task
        if path == 'projects': return {'projects': [{'name': 'studio', 'project': project, 'conversations': [{'id': 'preview-chat', 'goal': task['prompt']}]}]}
        if path.startswith('conversation?'): return conversation
        if path.startswith('project?'): return {'requirements': 'Preserve public APIs. Run the relevant tests.', 'indexed_files': 428, 'eligible_files': 428, 'turns': 3, 'guidance_files': [{'name': 'CODING_HUB.md'}]}
        if path == 'quota/refresh': return status['quota']
        if path == 'free-quota/refresh': return status['free_quota']
        raise AssertionError('Unexpected request: ' + path)

    def settle(seconds=.35):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            time.sleep(.01)

    with tempfile.TemporaryDirectory(prefix='coding-hub-ui-') as temporary:
        old_state = hub.STATE
        hub.STATE = Path(temporary)
        app = desktop.create_application(Gtk, Gdk, Gio, GLib, api, 'http://127.0.0.1:8765/')
        try:
            app.register(None)
            app.activate()
            settle(.7)
            app.project.set_text(project)
            assert not app.error.get_visible(), app.error.get_text()
            assert app.connection.get_text() == '●  Connected locally'
            assert not app.release_button.get_sensitive()
            for width in (1240, 980):
                app.window.set_default_size(width, 840)
                for page in ('workspace', 'usage', 'models', 'history', 'memory'):
                    app.stack.set_visible_child_name(page)
                    settle()
                    assert app.navigation[page].has_css_class('selected')
                    assert app.window.get_width() <= width, (page, width, app.window.get_width())
                    paintable = Gtk.WidgetPaintable.new(app.window)
                    snapshot = Gtk.Snapshot()
                    paintable.snapshot(snapshot, app.window.get_width(), app.window.get_height())
                    node = snapshot.to_node()
                    assert node is not None, 'Empty native render'
                    texture = app.window.get_renderer().render_texture(node, None)
                    texture.save_to_png(str(args.output / f'desktop-{page}-{width}.png'))
            app.open_chat(project, 'preview-chat')
            settle()
            assert app.conversation == 'preview-chat'
            assert not app.project.get_editable()
            assert app.chat_scroll.get_visible()
            app.new_task()
            assert app.project.get_editable() and not app.conversation
            app.select_history(task)
            settle()
            assert app.output_details.get_expanded()
            assert app.activity.get_text().startswith('Completed')
            child = app.free_quota_cards.get_first_child().get_child()
            detail = child.get_last_child()
            detail.set_expanded(True)
            app.render_free_quota(status['free_quota'])
            assert detail.get_expanded(), 'Refresh collapsed the token breakdown'
            app.render_free_quota({'models': [], 'error': 'Usage unavailable', 'local': {}})
            assert 'unavailable' in app.free_quota_checked.get_text()
            print(json.dumps({'rendered': 10, 'native_workflows': 'passed', 'provider_requests': 0}))
        finally:
            app.closed(app.window)
            app.window.destroy()
            app.quit()
            hub.STATE = old_state


if __name__ == '__main__':
    main()

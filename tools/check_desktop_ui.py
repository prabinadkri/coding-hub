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
              'models': [hub.LOCAL_AGENT_MODEL, 'qwen3:8b'], 'free_models': list(hub.FREE_MODELS), 'local_ready': True, 'local_model': hub.LOCAL_AGENT_MODEL,
              'gpu': {'name': 'NVIDIA GeForce GTX 1650', 'used_mb': 128, 'total_mb': 4096, 'utilization': 17},
              'cpu': 24.5, 'sampled_at': now, 'accounts': [{'id': p, 'installed': True} for p in ('antigravity','claude','openai')],
              'provider_models': {'claude': [{'id':'sonnet','name':'Claude Sonnet'}]},
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
                    'turns': [{'rowid': 1, 'task': 'preview-task', 'has_review': True, 'request': task['prompt'], 'result': '## Changes\n- Clear navigation and grouped metrics.\n- Compact replies with **persistent memory**.\n\n## Checks\nLayout checks passed.\n\n```python\nprint("Ready to build")\n```', 'status': 'completed'}]}
    sample_conversation = json.loads(json.dumps(conversation))
    requests = []

    def api(path, body=None):
        requests.append((path, body))
        if path == 'accounts/start': return {'id':'preview-signin','running':True,'screen':'Choose Google OAuth to continue','provider':'antigravity','links':[]}
        if path.startswith('accounts/session'): return {'id':'preview-signin','running':True,'screen':'Choose Google OAuth to continue','links':[]}
        if path in ('accounts/input','accounts/close'): return {'ok':True}
        if path.startswith('changes?'): return {'files':[{'path':'src/dashboard.py','status':'modified','added':1,'removed':1,'diff':'--- a/src/dashboard.py\n+++ b/src/dashboard.py\n@@ -1 +1 @@\n-old_layout()\n+compact_layout()\n'}], 'limited':False, 'note':'Source files changed during this task.'}
        if path == 'status': return status
        if path == 'tasks':
            if body: return dict(task, id='preview-send', prompt=body['prompt'], status='running')
            return {'tasks': [task]}
        if path.startswith('tasks/'): return task
        if path == 'projects': return {'projects': [{'name': 'studio', 'project': project, 'conversations': [{'id': 'preview-chat', 'goal': task['prompt']}]}]}
        if path.startswith('conversation?'): return conversation
        if path == 'project/notes': return {'saved': True}
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
        browser_requests = []
        def browser_launcher(Gtk, Gdk, Gio, parent, url, finished):
            browser_requests.append(url)
            finished(True, None)
        app = desktop.create_application(Gtk, Gdk, Gio, GLib, api, 'http://127.0.0.1:8765/', browser_launcher=browser_launcher)
        try:
            app.register(None)
            app.activate()
            settle(.7)
            app.project.set_text(project)
            assert not app.error.get_visible(), app.error.get_text()
            assert app.connection.get_text() == '●  Connected locally'
            assert not app.release_button.get_sensitive()
            app.open_web_button.emit('clicked')
            assert browser_requests == ['http://127.0.0.1:8765/']
            assert app.open_web_button.get_sensitive()
            assert not app.error.get_visible()
            for width in (1240, 980):
                app.window.set_default_size(width, 840)
                for page in ('workspace', 'usage', 'models', 'history', 'memory', 'accounts'):
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
            assert not app.project_card.get_visible()
            assert app.chat_memory_button.get_visible()
            assert not app.chat_settings.get_expanded()
            assert app.chat_scroll.get_visible()
            for width in (1240, 980):
                app.window.set_default_size(width, 840)
                settle()
                user_row = app.chat_messages.get_first_child()
                assistant_row = user_row.get_next_sibling()
                user_card = user_row.get_last_child()
                assistant_card = assistant_row.get_first_child()
                assert user_card.has_css_class('user-message')
                assert assistant_card.has_css_class('assistant-message')
                assert user_card.get_halign() == Gtk.Align.END
                assert assistant_card.get_halign() == Gtk.Align.FILL
                assert user_card.get_width() < app.chat_scroll.get_width() - 50
                assert app.chat_scroll.get_width() * .85 < assistant_card.get_width() < app.chat_scroll.get_width() - 12
                actions = assistant_card.get_last_child()
                assert actions.get_first_child().get_height() < 30, 'Reply actions should remain subtle'
                assert not app.composer_heading.get_visible()
                assert app.prompt_placeholder.get_visible()
                paintable = Gtk.WidgetPaintable.new(app.window)
                snapshot = Gtk.Snapshot()
                paintable.snapshot(snapshot, app.window.get_width(), app.window.get_height())
                texture = app.window.get_renderer().render_texture(snapshot.to_node(), None)
                texture.save_to_png(str(args.output / f'desktop-chat-{width}.png'))
            app.open_changes('preview-task')
            settle()
            assert app.review_window.get_visible()
            paintable = Gtk.WidgetPaintable.new(app.review_window)
            snapshot = Gtk.Snapshot()
            paintable.snapshot(snapshot, app.review_window.get_width(), app.review_window.get_height())
            texture = app.review_window.get_renderer().render_texture(snapshot.to_node(), None)
            texture.save_to_png(str(args.output / 'desktop-changes.png'))
            app.review_window.close()
            app.window.present()
            settle()
            app.route.set_selected(2)
            assert app.model_row.get_visible()
            app.model_picker.set_selected(1)
            assert app.form()['model'] == hub.FREE_MODELS[0]
            app.route.set_selected(3)
            app.model_picker.set_selected(2)
            assert app.form()['model'] == 'qwen3:8b'
            app.route.set_selected(0)
            assert app.form()['model'] is None
            app.project.set_text('/workspace/other-project')
            assert app.form()['project'] == project, 'A follow-up must keep its conversation project'
            app.stack.set_visible_child_name('memory')
            settle()
            assert app.memory_project == project
            app.save_requirements()
            settle()
            saved = next(body for path, body in reversed(requests) if path == 'project/notes')
            assert saved['project'] == project, 'Memory must save to the project shown in the editor'
            app.new_task()
            assert app.project.get_editable() and not app.conversation
            assert app.project_card.get_visible()
            assert app.chat_settings.get_expanded()
            app.project.set_text(project)
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
            app.open_chat(project, 'preview-chat')
            settle()
            conversation['turns'] = [dict(conversation['turns'][0], rowid=i, request=f'Question {i}', result='A detailed answer. ' * 18) for i in range(1,25)]
            conversation['total_turns'] = 24
            app.render_messages(conversation)
            settle()
            adj = app.chat_scroll.get_vadjustment()
            assert adj.get_upper() > adj.get_page_size(), 'Long conversation should scroll'
            assert abs(adj.get_value()-(adj.get_upper()-adj.get_page_size())) < 2, 'Open chat must show newest message'
            adj.set_value(0)
            assert not app.follow_chat
            conversation['turns'].append(dict(conversation['turns'][0], rowid=25))
            conversation['total_turns'] = 25
            app.render_messages(conversation)
            settle()
            assert adj.get_value() < 2, 'Refresh must not pull someone away from older messages'
            assert app.latest_button.get_visible()
            app.jump_to_latest()
            settle()
            assert abs(adj.get_value()-(adj.get_upper()-adj.get_page_size())) < 2
            assert app.chat_scroll.get_height() > app.composer.get_height()
            assert app.prompt_scroll.get_height() < 80, 'Reply input should start compact'
            app.prompt.grab_focus()
            app.prompt.get_buffer().set_text('Keyboard shortcut test')
            assert app.keyboard.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
            sent_before = len([p for p,b in requests if p=='tasks' and b])
            handled = app.keyboard.emit('key-pressed', Gdk.KEY_Return, 0, Gdk.ModifierType.CONTROL_MASK)
            settle()
            assert handled, 'Ctrl+Enter must be intercepted before TextView consumes it'
            assert len([p for p,b in requests if p=='tasks' and b]) == sent_before+1
            assert not app.prompt.get_buffer().get_char_count()
            app.theme_switch.set_active(True)
            assert json.loads(app.preferences_path.read_text())['dark'] is True
            assert app.window.has_css_class('dark')
            app.tasks = [task]
            app.selected = task['id']
            app.show_task(task)
            app.message_archive = {}
            app.message_signature = None
            app.render_messages(sample_conversation)
            app.jump_to_latest()
            settle()
            for page in ('workspace','models','accounts'):
                app.stack.set_visible_child_name(page)
                settle()
                paintable = Gtk.WidgetPaintable.new(app.window)
                snap = Gtk.Snapshot()
                paintable.snapshot(snap, app.window.get_width(), app.window.get_height())
                texture = app.window.get_renderer().render_texture(snap.to_node(), None)
                texture.save_to_png(str(args.output / f'desktop-dark-{page}.png'))
            app.route.set_selected(1)
            app.start_signin('antigravity')
            settle()
            assert app.signin_panel.get_visible()
            app.signin_input.set_text('synthetic-code')
            app.signin_text()
            settle()
            assert not app.signin_input.get_text()
            assert any(p=='accounts/input' and b.get('text')=='synthetic-code' for p,b in requests)
            app.close_signin()
            settle()
            assert not app.signin_panel.get_visible()
            print(json.dumps({'rendered': 18, 'native_workflows': 'passed', 'provider_requests': 0}))
        finally:
            app.closed(app.window)
            app.window.destroy()
            app.quit()
            hub.STATE = old_state


if __name__ == '__main__':
    main()

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
    posted_tasks = []
    deleted_chats = set()

    def api(path, body=None):
        requests.append((path, body))
        if path == 'accounts/start': return {'id':'preview-signin','running':True,'screen':'Choose Google OAuth to continue','provider':'antigravity','links':[]}
        if path.startswith('accounts/session'): return {'id':'preview-signin','running':True,'screen':'Choose Google OAuth to continue','links':[]}
        if path in ('accounts/input','accounts/close'): return {'ok':True}
        if path.startswith('changes?'): return {'files':[{'path':'src/dashboard.py','status':'modified','added':1,'removed':1,'diff':'--- a/src/dashboard.py\n+++ b/src/dashboard.py\n@@ -1 +1 @@\n-old_layout()\n+compact_layout()\n'}], 'limited':False, 'note':'Source files changed during this task.'}
        if path == 'diagnostics': return {'text':'[OK] System disk\n20 GB free\n[OK] NVIDIA GPU\nGTX 1650'}
        if path == 'status': return status
        if path == 'conversation/delete':
            deleted_chats.add(body['id'])
            return {'deleted': body['id'], 'task_ids': ['preview-task']}
        if path == 'tasks':
            if body:
                value = dict(task, id='preview-send', conversation=body.get('conversation') or 'preview-empty-chat', scope=body.get('scope','project'), project=str(hub.general_workspace()) if body.get('scope')=='general' else body['project'], prompt=body['prompt'], status='running', created_at=time.time(), started_at=time.time(), ended_at=None)
                posted_tasks[:] = [value]
                return value
            return {'tasks': [t for t in posted_tasks + [task] if t['conversation'] not in deleted_chats]}
        if path.startswith('tasks/'): return next((t for t in posted_tasks if path.endswith(t['id'])),task)
        if path == 'projects': return {'projects': [{'name': 'studio', 'project': project, 'conversations': [] if 'preview-chat' in deleted_chats else [{'id': 'preview-chat', 'goal': task['prompt']}]}]}
        if path.startswith('conversation?'):
            if 'preview-empty-chat' in path:
                current = posted_tasks[0] if posted_tasks else {'project':project,'prompt':'Create a page'}
                return {'id':'preview-empty-chat','scope':current.get('scope','project'),'project':current['project'],'goal':current['prompt'],'turns':[],'total_turns':0}
            return conversation
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

    def capture(window, name):
        # A widget can have no render node while GTK replaces its current frame.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            paintable = Gtk.WidgetPaintable.new(window)
            snapshot = Gtk.Snapshot()
            paintable.snapshot(snapshot, window.get_width(), window.get_height())
            node = snapshot.to_node()
            if node is not None:
                window.get_renderer().render_texture(node, None).save_to_png(str(args.output/name))
                return
            settle(.1)
        raise AssertionError('Empty native render: ' + name)

    with tempfile.TemporaryDirectory(prefix='coding-hub-ui-') as temporary:
        old_state = hub.STATE
        hub.STATE = Path(temporary)
        browser_requests = []
        def browser_launcher(Gtk, Gdk, Gio, parent, url, finished):
            browser_requests.append(url)
            finished(True, None)
        app = desktop.create_application(Gtk, Gdk, Gio, GLib, api, 'http://127.0.0.1:8765/', browser_launcher=browser_launcher)
        try:
            Gtk.Settings.get_default().set_property('gtk-theme-name', 'Adwaita-dark')
            app.register(None)
            app.activate()
            settle(.7)
            app.scope.set_selected(1)
            app.project.set_text(project)
            assert not app.error.get_visible(), app.error.get_text()
            assert Gtk.Settings.get_default().get_property('gtk-theme-name') == 'Adwaita'
            assert not app.sidebar_tools.get_expanded()
            assert app.workspace_split.get_position() == 248
            assert app.sidebar.get_width() <= 260, app.sidebar.get_width()
            app.workspace_split.set_position(300)
            settle(.5)
            assert app.workspace_split.get_position() == 300
            assert json.loads(app.preferences_path.read_text())['sidebar_width'] == 300
            app.theme_switch.set_active(True)
            assert json.loads(app.preferences_path.read_text())['sidebar_width'] == 300
            app.theme_switch.set_active(False)
            app.workspace_split.set_position(248)
            settle(.5)
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
                    capture(app.window, f'desktop-{page}-{width}.png')
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
                capture(app.window, f'desktop-chat-{width}.png')
            app.open_changes('preview-task')
            settle()
            assert app.review_window.get_visible()
            capture(app.review_window, 'desktop-changes.png')
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
            assert not app.project_card.get_visible()
            assert app.scope.get_selected() == 0
            app.scope.set_selected(1)
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
            posted_tasks.clear()
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
                capture(app.window, f'desktop-dark-{page}.png')
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
            # Regression: switch from an archived conversation to a truly empty project.
            app.theme_switch.set_active(False)
            app.open_chat(project, 'preview-chat')
            settle()
            assert app.message_archive
            app.new_task()
            assert not app.message_archive and app.archive_conversation is None
            assert not app.sidebar_tools.get_expanded()
            empty = Path(temporary) / 'empty-project'
            empty.mkdir()
            app.scope.set_selected(1)
            app.project.set_text(str(empty))
            app.prompt.get_buffer().set_text('Create a simple page in this empty project')
            app.run_task()
            settle(.7)
            assert app.conversation == 'preview-empty-chat'
            assert not app.error.get_visible(), app.error.get_text()
            assert app.pending_indicator and app.pending_indicator[1].get_text().startswith('Thinking')
            assert not app.message_archive
            assert app.form()['project'] == str(empty)
            assert not list(empty.iterdir()), 'The UI fixture must not generate source files'
            # A stale total with an empty page must not index an empty list.
            app.message_signature = None
            app.render_messages({'id':'preview-empty-chat','goal':'Create a page','turns':[],'total_turns':8})
            detail = dict(posted_tasks[0], progress={'label':'Writing a file…','detail':'Current action: Write file · running'})
            app.tasks = [detail]
            app.render_messages({'id':'preview-empty-chat','goal':'Create a page','turns':[],'total_turns':8})
            assert app.pending_indicator[1].get_text() == 'Writing a file…'
            app.render_tree([{'name':'empty-project','project':str(empty),'conversations':[{'id':'preview-empty-chat','goal':'Create a page'}]}])
            group = app.project_tree.get_first_child()
            assert group.get_child().get_first_child().get_label() == '+ New chat'
            settle()
            capture(app.window, 'desktop-working-light.png')
            assert app.sidebar_tree_scroll.get_height() > 350, 'Project chats must dominate the sidebar'
            app.tasks = [dict(detail,status='completed')]
            app.render_messages({'id':'preview-empty-chat','goal':'Create a page','turns':[],'total_turns':0})
            assert app.pending_indicator is None, 'Completed tasks must remove the running indicator'
            posted_tasks.clear()
            app.new_task()
            assert app.output_details.get_label() == 'Task activity'
            app.scope.set_selected(1)
            app.project.set_text(str(empty))
            settle()
            capture(app.window, 'desktop-new-chat-light.png')
            app.new_task()
            assert app.form()['scope'] == 'general'
            assert not app.project_card.get_visible()
            app.prompt.get_buffer().set_text('Explain my Linux memory usage')
            app.run_task()
            settle(.7)
            assert app.scope.get_selected() == 0
            assert app.chat_subtitle.get_text().startswith('General task')
            assert posted_tasks[0]['scope'] == 'general'
            assert not app.error.get_visible(), app.error.get_text()
            posted_tasks.clear()
            app.new_task()
            settle()
            capture(app.window,'desktop-general-light.png')
            app.stack.set_visible_child_name('models')
            app.diagnose_system()
            settle(.7)
            assert 'GTX 1650' in app.doctor_report.get_text()
            app.discuss_diagnostics()
            assert app.form()['scope'] == 'general' and not app.edits.get_active()
            assert 'GTX 1650' in app.form()['prompt']
            app.open_chat(project, 'preview-chat')
            settle()
            group = app.project_tree.get_first_child()
            row = group.get_child().get_first_child().get_next_sibling()
            assert row.get_last_child().get_tooltip_text().startswith('Delete chat:')
            remove = row.get_last_child()
            assert remove.get_opacity() == 0, 'Trash should be hidden until hover or focus'
            controllers = row.observe_controllers()
            motion = next(controllers.get_item(i) for i in range(controllers.get_n_items()) if isinstance(controllers.get_item(i), Gtk.EventControllerMotion))
            motion.emit('enter', 10.0, 10.0)
            assert remove.get_opacity() == 1
            motion.emit('leave')
            assert remove.get_opacity() == 0
            remove.grab_focus()
            settle()
            assert remove.get_opacity() == 1, 'Keyboard focus must reveal the delete action'
            app.prompt.grab_focus()
            settle()
            assert remove.get_opacity() == 0
            remove.emit('clicked')
            settle()
            capture(app.delete_dialog, 'desktop-delete-light.png')
            app.delete_dialog.response(Gtk.ResponseType.CANCEL)
            settle()
            assert not any(p == 'conversation/delete' for p, b in requests)
            assert app.conversation == 'preview-chat'
            app.theme_switch.set_active(True)
            app.confirm_delete_chat(project, 'preview-chat', task['prompt'])
            settle()
            capture(app.delete_dialog, 'desktop-delete-dark.png')
            app.delete_dialog.response(Gtk.ResponseType.ACCEPT)
            settle(.7)
            assert any(p == 'conversation/delete' and b['id'] == 'preview-chat' for p, b in requests)
            assert app.conversation is None and not app.message_archive
            assert not app.tasks and not app.deleting_chat
            assert not app.error.get_visible(), app.error.get_text()
            assert json.loads(app.draft_path.read_text())['conversation'] is None
            print(json.dumps({'rendered': 23, 'native_workflows': 'passed', 'provider_requests': 0}))
        finally:
            app.closed(app.window)
            app.window.destroy()
            app.quit()
            hub.STATE = old_state


if __name__ == '__main__':
    main()

"""On-demand native workspace tools; shares the authenticated dashboard API."""
import json
from urllib.parse import quote


def open_tool(app, Gtk, api, name):
    title = {'checks': 'Project checks', 'queue': 'Task queue', 'metrics': 'Model observations'}[name]
    window = Gtk.Window(title=title, transient_for=app.window, modal=True, default_width=740, default_height=740)
    window.add_css_class('coding-hub')
    if app.theme_switch.get_active(): window.add_css_class('dark')
    app.feature_window = window
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    for side in ('top', 'bottom', 'start', 'end'): getattr(outer, 'set_margin_' + side)(20)
    heading = app.row(); label = app.label(title, 'page-title'); label.set_hexpand(True)
    heading.append(label); heading.append(app.button('Close', lambda *_: window.close())); outer.append(heading)
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True); scroll.set_child(body); outer.append(scroll)
    window.set_child(outer); window.present()
    app.tools_popover.popdown()
    def clear():
        while body.get_first_child(): body.remove(body.get_first_child())
    def note(text):
        label = app.label(text, 'muted'); label.set_wrap(True); body.append(label); return label
    def button(text, callback):
        return app.button(text, callback)
    def reload(*_):
        clear(); note('Loading…')
        app.background(lambda: api(name if name != 'checks' else 'checks?project=' + quote(app.current_project())), loaded)
    def loaded(data):
        clear()
        if name == 'queue':
            note('Queue paused. Waiting tasks survive restarts.' if data['paused'] else 'One task runs at a time. New messages join this queue.')
            actions = app.row()
            def control(payload): app.background(lambda: api('queue', payload), loaded)
            actions.append(button('Resume queue' if data['paused'] else 'Pause queue', lambda *_: control({'action':'resume' if data['paused'] else 'pause'})))
            actions.append(button('Refresh', reload)); body.append(actions)
            if not data['tasks']: note('No waiting or running tasks.')
            for task in data['tasks']:
                card = app.card(10); card.append(app.label(task['prompt'][:180], 'section-title'))
                card.append(app.label(task['backend'] + ' · ' + task['status'] + '\n' + task['project'], 'muted'))
                controls = app.row()
                if task['status'] == 'queued':
                    for direction in ('up', 'down'):
                        controls.append(button('Move ' + direction, lambda *_, t=task, d=direction: control({'action':'move','id':t['id'],'direction':d})))
                controls.append(button('Cancel waiting task' if task['status'] == 'queued' else 'Stop task',
                    lambda *_, t=task: app.background(lambda: api('stop', {'id':t['id']}), lambda _: reload())))
                card.append(controls); body.append(card)
        elif name == 'metrics':
            note(data['note'])
            resource = data['local_resource']
            note('Local slot: ' + ('busy · ' + resource.get('owner', 'another app') if resource.get('busy') else 'available') + '. Cooperating clients only.')
            from datetime import datetime
            checked = data['catalog'].get('checked_at')
            note('Catalog checked: ' + (datetime.fromtimestamp(checked).strftime('%c') if checked else 'not yet checked'))
            for candidate in data['catalog'].get('candidates', []):
                note(candidate['model'] + ' · ' + ('verified zero cost' if candidate['free'] else 'unavailable / price not approved · ' + candidate['status']))
            if not data['observations']: note('No observed model calls in this version yet.')
            for row in data['observations']:
                card = app.card(10); card.append(app.label(row['model'], 'section-title'))
                card.append(app.label(f"{row['samples']} attempts · {row['successful']} completed · {row['average_seconds']}s average\nChecks: {row['checks_passed']} passed / {row['checks_failed']} failed", 'muted'))
                card.append(app.label('Last success: ' + (datetime.fromtimestamp(row['last_success']).strftime('%c') if row['last_success'] else 'none'), 'muted'))
                body.append(card)
            note('Observed cooldowns: ' + json.dumps(data['cooldowns'], ensure_ascii=False))
            body.append(button('Refresh', reload))
        else:
            project = app.current_project()
            note(project)
            note('Trusted commands run independently before and after coding tasks. One command per line; shell operators need an explicit shell.')
            def text_field(title, value, height=100):
                note(title)
                editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True)
                editor.get_buffer().set_text(value)
                scroller = Gtk.ScrolledWindow(min_content_height=height, max_content_height=height, propagate_natural_height=True)
                scroller.set_child(editor); body.append(scroller)
                return editor
            def entry(title, value):
                note(title); widget = Gtk.Entry(text=str(value)); body.append(widget); return widget
            def text(editor):
                buf=editor.get_buffer(); return buf.get_text(buf.get_start_iter(),buf.get_end_iter(),True)
            commands = text_field('Check commands', '\n'.join(data['commands']))
            numbers = app.row(20)
            def number_field(label, value, minimum, maximum):
                column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
                column.append(app.label(label, 'muted'))
                control = Gtk.SpinButton.new_with_range(minimum, maximum, 1)
                control.set_value(value); column.append(control); numbers.append(column)
                return control
            repairs = number_field('Free repair attempts', data['repairs'], 0, 2)
            timeout = number_field('Timeout per command (seconds)', data['timeout'], 5, 600)
            body.append(numbers)
            automatic = Gtk.CheckButton(label='Automatically test web apps after coding changes')
            automatic.set_active(data['browser'].get('automatic', True)); body.append(automatic)
            url = entry('Browser test · localhost URL (optional)', data['browser']['url'])
            url.set_placeholder_text('Leave blank to discover and start this project')
            command = entry('Start command (optional, requires URL)', data['browser'].get('command', ''))
            steps = text_field('Browser steps (JSON)', json.dumps(data['browser']['steps'], indent=2), 140)
            note('No steps needed for automatic desktop/mobile checks and local navigation. Add steps for specific flows. Example: [{"action":"text","selector":"h1","value":"Welcome"}]. You can also click Test website in chat. Traces may contain displayed app data.')
            output = app.label('', 'muted'); output.set_selectable(True); output.set_wrap(True)
            actions = app.row()
            def perform(action):
                try:
                    config={'commands':[s.strip() for s in text(commands).splitlines() if s.strip()],
                            'repairs':repairs.get_value_as_int(), 'timeout':timeout.get_value_as_int(),
                            'browser':{'url':url.get_text().strip(),'steps':json.loads(text(steps)), 'command':command.get_text().strip(), 'automatic':automatic.get_active()}}
                except (ValueError, TypeError) as error:
                    output.set_text(str(error)); return
                output.set_text('Saving…' if action == 'save' else 'Running checks…')
                def work():
                    api('checks', {'project':project,'config':config})
                    return {'status':'Saved'} if action == 'save' else api(action, {'project':project})
                def done(result):
                    summary = result.get('status', '')
                    if 'checks' in result:
                        summary += '\n' + '\n\n'.join(c['command'] + ' · exit ' + str(c['exit_code']) + '\n' + c['output'][-2500:] for c in result['checks'])
                    if 'directory' in result:
                        summary += '\n' + result.get('error', '') + '\nArtifacts: ' + result['directory'] + '\nReplay: npx playwright show-trace ' + result['directory'] + '/trace.zip'
                        if result.get('console_errors'): summary += '\n' + '\n'.join(result['console_errors'])
                    output.set_text(summary)
                app.background(work, done)
            for label, action in (('Save checks', 'save'), ('Run checks', 'verify'), ('Test application', 'browser-test')):
                actions.append(button(label, lambda *_, a=action: perform(a)))
            body.append(actions); body.append(output)
            latest=data.get('browser_latest')
            if latest: output.set_text('Last browser run: ' + latest['status'] + '\nArtifacts: ' + latest['directory'])
    reload()
    return window


def open_browser_report(app, Gtk, api, project, identifier):
    window = Gtk.Window(title='Browser report', transient_for=app.window, default_width=820, default_height=760)
    window.add_css_class('coding-hub')
    if app.theme_switch.get_active(): window.add_css_class('dark')
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    for side in ('top', 'bottom', 'start', 'end'): getattr(body, 'set_margin_' + side)(20)
    scroll = Gtk.ScrolledWindow(vexpand=True); scroll.set_child(body); window.set_child(scroll)
    body.append(app.button('Close', lambda *_: window.close()))
    label = app.label('Loading browser report…', 'muted'); label.set_selectable(True); body.append(label)
    def loaded(result):
        from browser_checks import summary
        from pathlib import Path
        label.set_text(summary(result))
        folder = Path(result['directory'])
        from gi.repository import Gio
        body.append(app.button('Open artifacts folder', lambda *_: Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)))
        for name in result.get('screenshots', []):
            body.append(app.label(name, 'muted'))
            picture = Gtk.Picture.new_for_filename(str(folder / name))
            picture.set_can_shrink(True); picture.set_size_request(-1, 240); body.append(picture)
    app.background(lambda: api('browser/report?project=' + quote(project) + '&id=' + identifier), loaded)
    window.present()
    return window

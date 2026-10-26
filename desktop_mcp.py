"""Native project MCP connection manager, using the same API as the browser."""
import json
from urllib.parse import quote


def open_connections(app, Gtk, api):
    project = app.current_project()
    scope = 'general' if not app.conversation and app.scope.get_selected() == 0 else 'project'
    window = Gtk.Window(title='MCP connections', transient_for=app.window, modal=True, default_width=760, default_height=720)
    window.add_css_class('coding-hub')
    if app.theme_switch.get_active(): window.add_css_class('dark')
    app.feature_window = window
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    for side in ('top', 'bottom', 'start', 'end'): getattr(outer, 'set_margin_' + side)(20)
    heading = app.row(); label = app.label('MCP connections', 'page-title'); label.set_hexpand(True)
    heading.append(label); heading.append(app.button('Close', lambda *_: window.close())); outer.append(heading)
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True); scroll.set_child(body); outer.append(scroll)
    window.set_child(outer); window.present(); app.tools_popover.popdown()
    state = {'generation': 0, 'closed': False}
    def close(*_): state.update(closed=True, generation=state['generation'] + 1); return False
    window.connect('close-request', close)
    def clear(box):
        while box.get_first_child(): box.remove(box.get_first_child())
    def note(text, parent=body):
        label = app.label(text, 'muted'); label.set_wrap(True); label.set_selectable(True); parent.append(label); return label
    def reload(*_):
        if state['closed']: return
        state['generation'] += 1; generation = state['generation']
        clear(body); output = note('Loading connections…')
        def done(value):
            if not state['closed'] and generation == state['generation']: loaded(value, generation)
        def failed(error):
            if not state['closed'] and generation == state['generation']: output.set_text(str(error))
        app.background(lambda: api('mcp?project=' + quote(project) + '&scope=' + scope), done, failed)
    def loaded(data, generation):
        nonlocal project
        project = data['project']
        clear(body); note(project); note(data['note'])
        output = app.label('', 'muted'); output.set_wrap(True); output.set_selectable(True)
        def active(): return not state['closed'] and state['generation'] == generation
        def request(payload, callback):
            output.set_text('Connecting…' if payload['action'] == 'test' else 'Saving…')
            app.background(lambda: api('mcp', {'project': project, **payload}),
                lambda result: callback(result) if active() else None,
                lambda error: output.set_text(str(error)) if active() else None)
        actions = app.row()
        if not any(v['name'] == 'github' for v in data['connections']):
            actions.append(app.button('Connect GitHub', lambda *_: request({'action': 'save', 'connection': {'name': 'github', 'type': 'github'}}, lambda _: reload())))
        actions.append(app.button('Add custom server', lambda *_: edit()))
        actions.append(app.button('Refresh', reload)); body.append(actions)
        if not data['connections']: note('No connections yet. Add GitHub or a trusted MCP server.')
        for item in data['connections']:
            card = app.card(10); card.append(app.label(item['name'], 'section-title'))
            note(('Enabled' if item['enabled'] else 'Disabled') + ' · ' + ('GitHub · read-only' if item['type'] == 'github' else item['type'] + ' · edits & commands mode'), card)
            controls = app.row()
            def test(*_, item=item):
                def done(result):
                    output.set_text('Connected to ' + item['name'] + (' as ' + result['identity'] if result.get('identity') else '') +
                        ' · ' + str(len(result['tools'])) + ' tools\n' + '\n'.join(t['name'] + ' — ' + t['description'] for t in result['tools']))
                request({'action': 'test', 'name': item['name']}, done)
            controls.append(app.button('Test connection', test))
            controls.append(app.button('Disable' if item['enabled'] else 'Enable', lambda *_, item=item: request({'action': 'disable' if item['enabled'] else 'enable', 'name': item['name']}, lambda _: reload())))
            controls.append(app.button('Edit', lambda *_, item=item: edit(item)))
            def remove(button, item=item):
                if button.get_label() != 'Confirm delete': button.set_label('Confirm delete'); return
                request({'action': 'delete', 'name': item['name']}, lambda _: reload())
            controls.append(app.button('Delete', remove)); card.append(controls); body.append(card)
        body.append(output)
        editor = app.card(12); editor.set_visible(False); body.append(editor)
        def edit(item=None):
            clear(editor); editor.set_visible(True)
            editor.append(app.label('Edit connection' if item else 'Custom MCP server', 'section-title'))
            note('JSON connection. command is an argument array; HTTP uses url. env/headers map to environment variable names, never secret values. tools optionally lists exact allowed tool names.', editor)
            text = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            def example(value): text.get_buffer().set_text(json.dumps(value, indent=2))
            example(item or {'name': 'my-server', 'type': 'stdio', 'command': ['/absolute/path/to/mcp-server'], 'env': {}, 'tools': []})
            if not item:
                examples = app.row()
                examples.append(app.button('Local example', lambda *_: example({'name': 'my-server', 'type': 'stdio', 'command': ['/absolute/path/to/mcp-server'], 'env': {'SERVER_API_KEY': 'MY_SERVER_KEY'}, 'tools': []})))
                examples.append(app.button('HTTP example', lambda *_: example({'name': 'my-server', 'type': 'http', 'url': 'https://example.com/mcp', 'headers': {'Authorization': 'MY_MCP_AUTH_HEADER'}, 'tools': []})))
                editor.append(examples)
            scroller = Gtk.ScrolledWindow(min_content_height=200, max_content_height=200, propagate_natural_height=True)
            scroller.set_child(text); editor.append(scroller)
            controls = app.row()
            def save(*_):
                try:
                    buffer = text.get_buffer(); value = json.loads(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))
                    if item and value.get('name') != item['name']: raise ValueError('Keep the existing name, or add a separate connection.')
                    request({'action': 'save', 'connection': value}, lambda _: reload())
                except (ValueError, AttributeError) as error: output.set_text(str(error))
            controls.append(app.button('Save connection', save)); controls.append(app.button('Cancel', lambda *_: editor.set_visible(False)))
            editor.append(controls); text.grab_focus()
    reload()
    return window

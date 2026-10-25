"""Read-only file previews reached from conversation references."""
import base64
from urllib.parse import urlencode


def open_reply_file(app, Gtk, Gdk, GLib, api, project, task, reference):
    window = Gtk.Window(title='Files', transient_for=app.window, default_width=820, default_height=700)
    app.file_window = window
    window.add_css_class('coding-hub')
    if app.theme_switch.get_active(): window.add_css_class('dark')
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for side in ('top', 'bottom', 'start', 'end'): getattr(outer, 'set_margin_' + side)(20)
    heading=app.row(); title=app.label('Files', 'page-title');title.set_hexpand(True);heading.append(title)
    heading.append(app.button('Close', lambda *_: window.close()));outer.append(heading)
    body=Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    scroll=Gtk.ScrolledWindow(vexpand=True,hexpand=True);scroll.set_child(body);outer.append(scroll)
    window.set_child(outer)
    state={'generation':0,'closed':False}
    window.connect('close-request', lambda *_: state.update(closed=True) or False)
    def clear():
        while body.get_first_child(): body.remove(body.get_first_child())
    def note(value, css='muted'):
        label=app.label(value,css);label.set_selectable(True);label.set_wrap(True);body.append(label);return label
    def load(path, offset=0):
        state['generation']+=1; generation=state['generation'];clear();note('Loading file preview…')
        query=urlencode({'project':project,'path':path,'task':task or '', 'offset':offset})
        def failed(error):
            if state['closed'] or generation!=state['generation']:return
            clear();note(str(error));note(path)
            body.append(app.button('Copy reference',lambda *_:Gdk.Display.get_default().get_clipboard().set(path)))
        def loaded(data):
            if state['closed'] or generation!=state['generation']:return
            clear();title.set_text(data['name'] or 'Files');note(data['path'])
            actions=app.row()
            if data.get('parent'):actions.append(app.button('Parent folder',lambda *_:load(data['parent'])))
            actions.append(app.button('Show in Files',lambda *_:app.background(
                lambda:api('files/reveal',{'project':project,'task':task,'path':data['path']}),
                lambda _:status.set_text('Opened in your file manager.'),lambda e:status.set_text(str(e)))))
            actions.append(app.button('Copy path',lambda *_:Gdk.Display.get_default().get_clipboard().set(data['path'])))
            actions.append(app.button('Refresh',lambda *_:load(path,offset)));body.append(actions)
            status=note(data.get('note',''))
            if data['kind']=='folder':
                if not data['entries']:note('This folder is empty or contains no previewable entries.')
                for entry in data['entries']:
                    button=app.button(('▸ ' if entry['kind']=='folder' else '')+entry['name'],lambda *_,e=entry:load(e['path']))
                    button.set_halign(Gtk.Align.FILL);button.set_tooltip_text(entry['path']);body.append(button)
                pages=app.row()
                if offset:pages.append(app.button('Previous',lambda *_:load(path,max(0,offset-100))))
                if data.get('has_more'):pages.append(app.button('Next',lambda *_:load(path,offset+100)))
                body.append(pages)
                if data.get('scan_limited'):note('Showing a bounded folder listing. Use Show in Files to see all entries.')
            elif data['kind']=='image':
                try:
                    raw=base64.b64decode(data['image'].split(',',1)[1])
                    texture=Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw));picture=Gtk.Picture.new_for_paintable(texture)
                    picture.set_can_shrink(True);picture.set_size_request(-1,280);body.append(picture)
                except (GLib.Error, ValueError):note('This image could not be previewed. Use Show in Files.')
            elif data['kind']=='text':
                if data.get('truncated'):note('Preview is shortened. Show in Files opens the full file location.')
                first=data.get('first_line')
                if first:note('Starting at line '+str(first)+((' · requested line '+str(data['line'])) if data.get('line') else ''))
                editor=Gtk.TextView(editable=False,monospace=True,wrap_mode=Gtk.WrapMode.NONE)
                editor.get_buffer().set_text(data['text'])
                if first and data.get('line'):
                    buffer=editor.get_buffer();number=data['line']-first
                    if 0<=number<buffer.get_line_count():
                        tag=buffer.create_tag('requested-line',weight=700,underline=1)
                        _,start=buffer.get_iter_at_line(number);end=start.copy();end.forward_to_line_end();buffer.apply_tag(tag,start,end)
                text_scroll=Gtk.ScrolledWindow(min_content_height=340,hexpand=True,vexpand=True)
                text_scroll.set_child(editor);body.append(text_scroll)
        app.background(lambda:api('files/preview?'+query),loaded,failed)
    load(reference);window.present()
    return window

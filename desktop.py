"""A native Linux application window sharing the local Coding Hub dashboard."""
from __future__ import annotations

import concurrent.futures
import json
import os
import signal
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

import dashboard
import hub


def ensure_server(port=8765):
    """Reuse a healthy server, or start one independently of the app window."""
    existing = dashboard.session_url()
    if existing:
        return existing
    directory = hub.private_dir(hub.STATE / "dashboard")
    with (directory / "server.log").open("a") as log:
        (directory / "server.log").chmod(0o600)
        process = subprocess.Popen(
            [sys.executable, "-u", str(hub.ROOT / "hub.py"), "web", "--no-browser", "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
            env=dict(os.environ, CODING_HUB_STATE=str(hub.STATE)))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        existing = dashboard.session_url()
        if existing:
            return existing
        if process.poll() is not None:
            break
        time.sleep(.15)
    raise RuntimeError("The dashboard could not start. Check " + str(directory / "server.log"))


def launch(port=8765):
    if not sys.platform.startswith("linux"):
        print("The native app requires Linux. Opening the browser interface on this platform.")
        return dashboard.serve(port)
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, Gdk, Gio, GLib
    except (ImportError, ValueError) as error:
        raise RuntimeError("Install python3-gi and gir1.2-gtk-4.0 for the native app. "
                           "You can also use 'codehub web' without these packages.") from error
    url = ensure_server(port)
    base, token = url.split("/#", 1)

    def api(path, body=None):
        headers = {"X-CodeHub-Token": token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + "/api/" + path,
            data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                raise RuntimeError(json.load(error).get("error", "Request failed.")) from None

    application = create_application(Gtk, Gdk, Gio, GLib, api, url)
    def quit_for_update():
        if application.window and not application.closed_window:
            application.closed(application.window)
        application.quit()
        return False
    signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(quit_for_update))
    return application.run([])


def create_application(Gtk, Gdk, Gio, GLib, api, url):
    """Build native widgets independently of transport for isolated UI validation."""
    from gi.repository import Pango
    routes = ("auto", "antigravity", "free", "local", "smart")

    class CodingHub(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="io.github.prabinadkri.CodingHub")
            self.window = None
            self.closed_window = False
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
            self.selected = None
            self.conversation = None
            self.conversation_project = ''
            self.tree_signature = None
            self.message_signature = None
            self.message_archive = {}
            self.archive_conversation = None
            self.tasks = []
            self.history_signature = None
            self.refreshing = False
            self.connected = False
            self.memory_project = None
            self.chat_layout = None
            self.last_output = None
            self.quota_signature = None
            self.free_signature = None
            self.draft_path = hub.STATE / "desktop" / "draft.json"

        @staticmethod
        def row(spacing=12):
            return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)

        @staticmethod
        def label(text, style=None):
            label = Gtk.Label(label=text, xalign=0)
            label.set_wrap(True)
            label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_max_width_chars(80)
            if style:
                label.add_css_class(style)
            return label

        def button(self, text, callback, style=None):
            button = Gtk.Button(label=text)
            button.connect("clicked", callback)
            if style:
                button.add_css_class(style)
            return button

        def card(self, spacing=12):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
            box.add_css_class('card')
            return box

        def flow(self, columns=3):
            grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                               column_spacing=14, row_spacing=14,
                               homogeneous=True, min_children_per_line=1,
                               max_children_per_line=columns)
            grid.set_activate_on_single_click(False)
            return grid

        def section_heading(self, title, action=None):
            row = self.row()
            label = self.label(title, 'section-title')
            label.set_hexpand(True)
            row.append(label)
            if action:
                row.append(action)
            return row

        def metric(self, title, value, note):
            box = self.card()
            box.set_size_request(230, -1)
            box.append(self.label(title, 'section'))
            number = self.label(value, 'quota-number')
            detail = self.label(note, 'muted')
            box.append(number)
            box.append(detail)
            return box, number, detail

        def do_activate(self):
            if self.window:
                self.window.present()
                return
            Gtk.Window.set_default_icon_name('coding-hub')
            self.window = Gtk.ApplicationWindow(application=self, title='Coding Hub')
            self.window.set_default_size(1240, 840)
            self.window.set_icon_name('coding-hub')
            self.window.connect('close-request', self.closed)
            css = Gtk.CssProvider()
            css.load_from_path(str(hub.ROOT / 'assets' / 'desktop.css'))
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            header = Gtk.HeaderBar()
            self.page_title = self.label('Workspace / Chats', 'header-title')
            self.page_title.set_wrap(False)
            header.set_title_widget(self.page_title)
            self.connection = self.label('Connecting…', 'muted')
            self.connection.set_wrap(False)
            header.pack_start(self.connection)
            header.pack_end(self.button('Open web ↗', lambda *_: webbrowser.open(url)))
            self.window.set_titlebar(header)
            keyboard = Gtk.EventControllerKey()
            keyboard.connect('key-pressed', self.shortcut)
            self.window.add_controller(keyboard)
            shell = self.row(0)
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=225)
            sidebar.add_css_class('sidebar')
            brand = self.row(10)
            picture = Gtk.Image.new_from_file(str(hub.ROOT / 'assets' / 'icon.svg'))
            picture.set_pixel_size(36)
            brand.append(picture)
            brand.append(self.label('Coding Hub', 'brand'))
            sidebar.append(brand)
            sidebar.append(self.label('YOUR CODING WORKSPACE', 'brand-caption'))
            new = self.button('+  New chat', self.new_task, 'new-chat')
            new.set_margin_top(18)
            new.set_margin_bottom(12)
            new.set_tooltip_text('New conversation · Ctrl+N')
            sidebar.append(new)
            sidebar.append(self.label('WORKSPACE', 'section'))
            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                   hhomogeneous=False, vhomogeneous=False, hexpand=True, vexpand=True)
            self.navigation = {}
            for name, title, icon in (
                ('workspace', 'Chats', 'user-available-symbolic'),
                ('history', 'Task history', 'document-open-recent-symbolic'),
                ('models', 'Models & hardware', 'computer-symbolic'),
                ('usage', 'Usage & limits', 'view-statistics-symbolic'),
                ('memory', 'Project memory', 'accessories-text-editor-symbolic')):
                button = self.button('', lambda _, page=name: self.stack.set_visible_child_name(page), 'nav-item')
                row = self.row(10)
                row.append(Gtk.Image.new_from_icon_name(icon))
                row.append(self.label(title))
                button.set_child(row)
                self.navigation[name] = button
                sidebar.append(button)
            project_heading = self.label('PROJECTS & CHATS', 'section')
            project_heading.set_margin_top(20)
            sidebar.append(project_heading)
            self.project_tree = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            tree_scroll = Gtk.ScrolledWindow(vexpand=True)
            tree_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            tree_scroll.set_child(self.project_tree)
            sidebar.append(tree_scroll)
            sidebar.append(self.label('Local workspace', 'sidebar-foot'))
            sidebar.append(self.label('Version ' + dashboard.VERSION, 'sidebar-foot'))
            shell.append(sidebar)
            main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.error = self.label('', 'error-banner')
            self.error.set_visible(False)
            main.append(self.error)
            main.append(self.stack)
            shell.append(main)
            self.window.set_child(shell)
            self.stack.connect('notify::visible-child-name', self.page_changed)
            body = self.page_box('New conversation', 'workspace', 'Choose a project and start a focused conversation.')
            self.chat_title = self.page_headings['workspace']
            self.chat_subtitle = self.page_subtitles['workspace']
            self.workspace_body = body
            self.chat_memory_button = self.button('Project memory', lambda *_: self.stack.set_visible_child_name('memory'))
            self.page_header_rows['workspace'].append(self.chat_memory_button)
            self.project_card = self.card()
            self.project_card.append(self.label('Project', 'section-title'))
            project_row = self.row()
            self.project = Gtk.Entry(hexpand=True, placeholder_text='Choose a project folder')
            self.project.set_tooltip_text('Project folder')
            project_row.append(self.project)
            self.browse_button = self.button('Browse…', self.browse)
            project_row.append(self.browse_button)
            self.project_card.append(project_row)
            body.append(self.project_card)
            self.chat_messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            self.chat_scroll = Gtk.ScrolledWindow(min_content_height=160, max_content_height=460, propagate_natural_height=True)
            self.chat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.chat_scroll.set_child(self.chat_messages)
            self.chat_scroll.set_visible(False)
            body.append(self.chat_scroll)
            composer = self.card(14)
            self.composer_title = self.label('Your message', 'section-title')
            composer_heading = self.row()
            self.composer_title.set_hexpand(True)
            composer_heading.append(self.composer_title)
            composer_heading.append(self.label('Ctrl+Enter to send', 'muted'))
            composer.append(composer_heading)
            self.prompt = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.prompt.set_tooltip_text('Describe what you want to build, fix, or understand')
            self.prompt_scroll = Gtk.ScrolledWindow(min_content_height=130)
            prompt_scroll = self.prompt_scroll
            prompt_scroll.add_css_class('input-frame')
            prompt_scroll.set_child(self.prompt)
            composer.append(prompt_scroll)
            options = self.row()
            route_label = self.label('Route', 'muted')
            route_label.set_wrap(False)
            options.append(route_label)
            self.route = Gtk.DropDown.new_from_strings(['Automatic', 'Antigravity', 'Free cloud', 'Local Qwen', 'Smart'])
            self.route.set_tooltip_text('Coding route')
            self.route.connect('notify::selected', self.route_changed)
            self.quality = Gtk.DropDown.new_from_strings(['Fast · Flash', 'Deep · Pro'])
            self.quality.set_tooltip_text('Antigravity quality')
            options.append(self.route)
            options.append(self.quality)
            settings_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            settings_body.append(options)
            self.hint = self.label('', 'muted')
            settings_body.append(self.hint)
            self.chat_settings = Gtk.Expander(label='Chat settings', expanded=True)
            self.chat_settings.set_child(settings_body)
            composer.append(self.chat_settings)
            self.route_changed()
            footer = self.row()
            self.edits = Gtk.CheckButton(label='Allow edits & commands')
            footer.append(self.edits)
            footer.append(Gtk.Box(hexpand=True))
            self.stop_button = self.button('Stop task', self.stop, 'danger')
            self.stop_button.set_sensitive(False)
            self.run_button = self.button('Send message  →', self.run_task, 'primary')
            footer.append(self.stop_button)
            footer.append(self.run_button)
            composer.append(footer)
            body.append(composer)
            self.activity = self.label('Ready · Analysis mode reads project files. Enable edits to implement changes.', 'muted')
            body.append(self.activity)
            self.output = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.output.add_css_class('output')
            output_scroll = Gtk.ScrolledWindow(min_content_height=220, vexpand=True)
            output_scroll.set_child(self.output)
            self.output_details = Gtk.Expander(label='Live task output')
            self.output_details.set_child(output_scroll)
            body.append(self.output_details)
            body.append(self.label('Closing this window keeps tasks running. Your chats are shared with the web interface.', 'footnote'))
            self.build_history_page()
            self.build_models_page()
            self.build_memory_page()
            self.build_quota_page()
            self.stack.set_visible_child_name('workspace')
            self.page_changed()
            try:
                draft = json.loads(self.draft_path.read_text())
                self.project.set_text(draft.get("project", str(Path.home() / "Documents")))
                self.conversation = draft.get("conversation")
                self.conversation_project = self.project.get_text() if self.conversation else ''
                self.project.set_editable(not bool(self.conversation))
                self.browse_button.set_sensitive(not bool(self.conversation))
                self.prompt.get_buffer().set_text(draft.get("prompt", ""))
                self.route.set_selected(routes.index(draft.get("backend", "auto")))
                self.quality.set_selected(1 if draft.get("quality") == "deep" else 0)
                self.edits.set_active(draft.get("mode") == "build")
            except (OSError, ValueError, TypeError):
                self.project.set_text(str(Path.home() / "Documents"))
            self.sync_chat_layout()
            self.window.present()
            hub.save_json(hub.STATE / "desktop" / "window.json", {"pid": os.getpid(), "status": "open", "opened_at": time.time()})
            print("Coding Hub native app window opened.", flush=True)
            self.refresh()
            GLib.timeout_add_seconds(3, self.refresh)
            GLib.timeout_add_seconds(5, self.save_draft)

        def background(self, work, callback):
            future = self.executor.submit(work)
            def done(result):
                def finish():
                    if self.closed_window:
                        return False
                    try:
                        callback(result.result())
                    except Exception as error:
                        print("App request failed: " + str(error), file=sys.stderr, flush=True)
                        self.error.set_text(str(error))
                        self.error.set_visible(True)
                        self.connection.set_text('Connection needs attention')
                        self.refreshing = False
                        self.run_button.set_sensitive(not any(t["status"] in dashboard.ACTIVE for t in self.tasks))
                    return False
                GLib.idle_add(finish)
            future.add_done_callback(done)

        def shortcut(self, controller, key, code, modifiers):
            if modifiers & Gdk.ModifierType.CONTROL_MASK:
                if key == Gdk.KEY_Return and self.run_button.get_sensitive():
                    self.run_task()
                    return True
                if key in (Gdk.KEY_n, Gdk.KEY_N):
                    self.new_task()
                    return True
            return False

        def form(self):
            buffer = self.prompt.get_buffer()
            return {"project": self.current_project(),
                    "prompt": buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
                    "backend": routes[self.route.get_selected()],
                    "quality": "deep" if self.quality.get_selected() == 1 else "fast",
                    "mode": "build" if self.edits.get_active() else "analysis", "conversation": self.conversation}

        def current_project(self):
            return self.conversation_project if self.conversation else self.project.get_text()

        def sync_chat_layout(self):
            existing = bool(self.conversation)
            self.project_card.set_visible(not existing)
            self.project.set_editable(not existing)
            self.browse_button.set_sensitive(not existing)
            self.composer_title.set_text('Reply' if existing else 'Your message')
            self.prompt_scroll.set_min_content_height(85 if existing else 130)
            if self.chat_layout != existing:
                self.chat_settings.set_expanded(not existing)
                self.output_details.set_expanded(False)
                self.chat_layout = existing
            if existing:
                self.workspace_body.add_css_class('conversation-page')
                self.chat_subtitle.set_text(Path(self.conversation_project).name + ' · Saved conversation')
                self.chat_subtitle.set_tooltip_text(self.conversation_project)
            else:
                self.workspace_body.remove_css_class('conversation-page')
                self.chat_subtitle.set_text('Choose a project and start a focused conversation.')
                self.chat_subtitle.set_tooltip_text(None)

        def save_draft(self):
            if self.closed_window:
                return False
            hub.save_json(self.draft_path, self.form())
            return True

        def route_changed(self, *_):
            selected = self.route.get_selected()
            if hasattr(self, 'chat_settings'):
                self.chat_settings.set_label('Chat settings · ' + ('Automatic', 'Antigravity', 'Free cloud', 'Local Qwen', 'Smart')[selected])
            if hasattr(self, "quality"):
                self.quality.set_sensitive(routes[selected] in ("auto", "antigravity", "smart"))
            if hasattr(self, "hint"):
                self.hint.set_text([
                    "Antigravity → verified free models → local Qwen. Continues from partial work if a provider fails.",
                    "Uses your Google sign-in. Fast selects Flash; Deep selects Pro. Provider quotas apply.",
                    "Checks current zero-cost pricing before each run. Provider quotas apply.",
                    "Qwen3 8B runs on this computer with 16K context. Best for focused tasks.",
                    "Antigravity plans and reviews; free/local workers implement. At most 2 manager calls. Savings and equal quality are not guaranteed; 6,000-byte request limit."
                ][selected])

        def browse(self, *_):
            dialog = Gtk.FileChooserNative.new("Choose a project", self.window,
                Gtk.FileChooserAction.SELECT_FOLDER, "Choose folder", "Cancel")
            current = Path(self.project.get_text()).expanduser()
            if current.is_dir():
                dialog.set_current_folder(Gio.File.new_for_path(str(current)))
            def response(chooser, result):
                if result == Gtk.ResponseType.ACCEPT:
                    chosen = chooser.get_file()
                    if chosen and chosen.get_path():
                        self.project.set_text(chosen.get_path())
                        self.save_draft()
                chooser.destroy()
            dialog.connect("response", response)
            dialog.show()
            self.folder_dialog = dialog

        def new_task(self, *_):
            self.conversation = None
            self.conversation_project = ''
            self.message_signature = None
            self.chat_scroll.set_visible(False)
            self.chat_title.set_text("New conversation")
            self.project.set_editable(True)
            self.browse_button.set_sensitive(True)
            self.stack.set_visible_child_name("workspace")
            self.selected = None
            self.prompt.get_buffer().set_text("")
            self.output.get_buffer().set_text("")
            self.last_output = None
            self.activity.set_text("Ready · Enter a task to begin.")
            self.error.set_visible(False)
            self.sync_chat_layout()
            self.output_details.set_expanded(False)
            self.stop_button.set_sensitive(False)
            self.save_draft()
            self.prompt.grab_focus()

        def run_task(self, *_):
            data = self.form()
            self.save_draft()
            self.error.set_visible(False)
            self.run_button.set_sensitive(False)
            def started(task):
                self.selected = task["id"]
                self.conversation = task['conversation']
                self.conversation_project = task['project']
                self.sync_chat_layout()
                self.project.set_editable(False)
                self.browse_button.set_sensitive(False)
                self.prompt.get_buffer().set_text('')
                self.save_draft()
                self.output_details.set_expanded(False)
                self.show_task(task)
                self.refresh()
            self.background(lambda: api("tasks", data), started)

        def stop(self, *_):
            if self.selected:
                identifier = self.selected
                self.stop_button.set_sensitive(False)
                self.background(lambda: api("stop", {"id": identifier}), self.show_task)

        def unload(self, *_):
            self.background(lambda: api("unload", {}), lambda _: self.refresh())

        def select_history(self, task):
            def loaded(detail):
                if detail.get('conversation'):
                    self.open_chat(detail['project'], detail['conversation'], detail)
                else:
                    self.new_task()
                    self.selected = detail['id']
                    self.project.set_text(detail['project'])
                    self.output_details.set_expanded(True)
                    self.show_task(detail)
            self.background(lambda: api('tasks/' + task['id']), loaded)

        def build_history_page(self):
            box = self.page_box('Task history', 'history', 'Review previous runs, results, and live task output.')
            self.history_cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.append(self.history_cards)

        def render_history(self):
            signature = json.dumps([(t['id'], t['status']) for t in self.tasks])
            if signature == self.history_signature:
                return
            self.history_signature = signature
            self.clear_box(self.history_cards)
            if not self.tasks:
                empty = self.card()
                empty.append(self.label('Your next idea starts here', 'section-title'))
                empty.append(self.label('Start a conversation. Its progress and result will appear here.', 'muted'))
                empty.append(self.button('New chat', self.new_task, 'primary'))
                self.history_cards.append(empty)
            for task in self.tasks:
                button = self.button('', lambda _, t=task: self.select_history(t), 'history-item')
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                head = self.row()
                title = self.label(task['prompt'].replace('\n', ' ')[:140], 'section-title')
                title.set_hexpand(True)
                head.append(title)
                head.append(self.label(task['status'].replace('_', ' ').capitalize(), 'pill'))
                card.append(head)
                stamp = datetime.fromtimestamp(task['created_at']).strftime('%d %b · %H:%M')
                card.append(self.label(Path(task['project']).name + '  ·  ' + task['backend'] + '  ·  ' + stamp, 'muted'))
                button.set_child(card)
                self.history_cards.append(button)

        def show_task(self, task):
            if task["id"] != self.selected:
                return
            elapsed = int((task.get("ended_at") or time.time()) - (task.get("started_at") or task["created_at"]))
            self.activity.set_text(f"{task['status'].capitalize()} · {elapsed // 60}m {elapsed % 60}s · {task['backend']} · {task['project']}")
            self.stop_button.set_sensitive(task["status"] in ("queued", "running"))
            output = task.get("output", "")
            if output != self.last_output:
                self.last_output = output
                buffer = self.output.get_buffer()
                buffer.set_text(output or "Waiting for the agent’s first update…")
                self.output.scroll_to_iter(buffer.get_end_iter(), 0, False, 0, 1)

        def refresh(self):
            if self.closed_window:
                return False
            if self.refreshing:
                return True
            self.refreshing = True
            selected = self.selected
            conversation, project = self.conversation, self.conversation_project
            def fetch():
                status = api("status")
                tasks = api("tasks")["tasks"]
                identifier = selected or next((t["id"] for t in tasks if t["status"] in dashboard.ACTIVE), None)
                detail = api("tasks/" + identifier) if identifier else None
                tree = api('projects')['projects']
                messages = api('conversation?project=' + quote(project) + '&id=' + conversation) if conversation else None
                return status, tasks, detail, tree, messages
            self.background(fetch, self.refreshed)
            return True

        def refreshed(self, result):
            self.refreshing = False
            status, self.tasks, detail, tree, messages = result
            self.render_tree(tree)
            self.render_quota(status.get('quota', {}))
            self.render_free_quota(status.get('free_quota', {}))
            if messages:
                self.render_messages(messages)
            if not status.get("checking"):
                if not self.connected:
                    self.connected = True
                    print("Native app connected: hardware status and shared task history loaded.", flush=True)
                gpu = status.get("gpu")
                self.hardware.set_text('NVIDIA driver active · Live hardware status' if gpu else 'CPU inference · Live hardware status')
            self.render_history()
            self.render_models(status)
            self.connection.set_text('●  Connected locally')
            self.run_button.set_sensitive(not any(t["status"] in dashboard.ACTIVE for t in self.tasks))
            if detail:
                if self.selected is None and detail["status"] in dashboard.ACTIVE:
                    self.selected = detail["id"]
                self.show_task(detail)

        @staticmethod
        def clear_box(box):
            child = box.get_first_child()
            while child:
                following = child.get_next_sibling()
                box.remove(child)
                child = following

        def render_tree(self, projects):
            signature = json.dumps(projects) + str(self.conversation)
            if signature == self.tree_signature:
                return
            self.tree_signature = signature
            expanded = {}
            child = self.project_tree.get_first_child()
            while child:
                if isinstance(child, Gtk.Expander):
                    expanded[child.get_tooltip_text()] = child.get_expanded()
                child = child.get_next_sibling()
            self.clear_box(self.project_tree)
            if not projects:
                note = self.label('Start a chat to add a project.', 'muted')
                note.set_wrap(True)
                self.project_tree.append(note)
            for project in projects:
                group = Gtk.Expander(label=project['name'])
                group.set_tooltip_text(project['project'])
                group.set_expanded(project['project'] == self.conversation_project or expanded.get(project['project'], len(projects) == 1))
                chats = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                chats.set_margin_start(12)
                for chat in project['conversations']:
                    title = chat['goal'].replace('\n', ' ')
                    button = self.button(title[:25] + ('…' if len(title) > 25 else ''),
                        lambda _, p=project['project'], c=chat['id']: self.open_chat(p, c))
                    button.set_tooltip_text(title)
                    if chat['id'] == self.conversation:
                        button.add_css_class('active-chat')
                    chats.append(button)
                def new_in_project(_, path=project['project']):
                    self.new_task()
                    self.project.set_text(path)
                    self.save_draft()
                chats.append(self.button('+ New chat', new_in_project))
                group.set_child(chats)
                self.project_tree.append(group)

        def open_chat(self, project, identifier, task=None):
            def opened(data):
                self.conversation = identifier
                self.conversation_project = project
                self.selected = task['id'] if task else None
                self.project.set_text(project)
                self.sync_chat_layout()
                self.project.set_editable(False)
                self.browse_button.set_sensitive(False)
                self.prompt.get_buffer().set_text('')
                self.message_signature = None
                self.render_messages(data)
                self.stack.set_visible_child_name('workspace')
                if task:
                    self.output_details.set_expanded(True)
                    self.show_task(task)
                self.save_draft()
                self.refresh()
            self.background(lambda: api('conversation?project=' + quote(project) + '&id=' + identifier), opened)

        def render_messages(self, data):
            if data['id'] != self.conversation:
                return
            if self.archive_conversation != data['id']:
                self.message_archive = {}
                self.archive_conversation = data['id']
            for turn in data['turns']:
                self.message_archive[turn['rowid']] = turn
            all_turns = [self.message_archive[key] for key in sorted(self.message_archive)]
            active = next((t for t in self.tasks if t.get('conversation') == self.conversation and t['status'] in dashboard.ACTIVE), None)
            signature = json.dumps(all_turns) + str(active.get('id') if active else '')
            if signature == self.message_signature:
                return
            self.message_signature = signature
            self.chat_title.set_text(data['goal'].split('\n')[0].split('. ')[0][:80])
            self.sync_chat_layout()
            self.chat_scroll.set_visible(True)
            self.clear_box(self.chat_messages)
            def add(role, text, status=''):
                user = role == 'You'
                row = self.row(0)
                row.add_css_class('message-row')
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                card.set_halign(Gtk.Align.END if user else Gtk.Align.START)
                card.add_css_class('user-message' if user else 'assistant-message')
                speaker = self.label(role, 'chat-title')
                speaker.set_xalign(1 if user else 0)
                card.append(speaker)
                content = self.label(text)
                content.set_selectable(True)
                content.set_max_width_chars(56 if user else 72)
                card.append(content)
                if status and status != 'completed':
                    card.append(self.label(status.replace('_', ' ').capitalize(), 'footnote'))
                gutter = Gtk.Box(hexpand=True, width_request=65)
                if user:
                    row.append(gutter)
                row.append(card)
                if not user:
                    row.append(gutter)
                self.chat_messages.append(row)
            if data['total_turns'] > len(all_turns):
                before = all_turns[0]['rowid']
                endpoint = 'conversation?project=' + quote(self.conversation_project) + '&id=' + self.conversation + '&before=' + str(before)
                self.chat_messages.append(self.button('Load earlier messages', lambda *_: self.background(lambda: api(endpoint), self.render_messages)))
            for turn in all_turns:
                add('You', turn['request'])
                add('Coding Hub', turn['result'] or 'No final response was saved.', turn['status'])
            if active:
                add('You', active['prompt'])
                add('Coding Hub', 'Working on your message. See live task output for progress.')

        def page_box(self, title, name, subtitle=''):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
            box.add_css_class('page')
            heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            title_row = self.row()
            label = self.label(title, 'heading')
            label.set_hexpand(True)
            title_row.append(label)
            heading.append(title_row)
            sublabel = self.label(subtitle, 'muted')
            if subtitle:
                heading.append(sublabel)
            box.append(heading)
            if not hasattr(self, 'page_headings'):
                self.page_headings = {}
                self.page_subtitles = {}
                self.page_header_rows = {}
            self.page_headings[name] = label
            self.page_subtitles[name] = sublabel
            self.page_header_rows[name] = title_row
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_child(box)
            self.stack.add_titled(scroll, name, title)
            return box

        def build_models_page(self):
            box = self.page_box('Models & hardware', 'models', 'Your cloud agents and local computing resources, in one place.')
            grid = self.flow()
            self.model_metrics = {}
            for key, title, value, note in (
                ('cloud', 'ANTIGRAVITY', 'Checking', 'Flash for speed · Pro for deeper work'),
                ('free', 'FREE CLOUD', 'Checking', 'Current pricing is verified before each task'),
                ('local', 'LOCAL QWEN 8B', 'Checking', 'Private inference · 16K context')):
                card, value_label, note_label = self.metric(title, value, note)
                self.model_metrics[key] = (value_label, note_label)
                grid.append(card)
            box.append(grid)
            hardware = self.card(18)
            self.release_button = self.button('Release local model', self.unload)
            hardware.append(self.section_heading('On your laptop', self.release_button))
            self.hardware = self.label('Checking agents and hardware…', 'muted')
            hardware.append(self.hardware)
            self.hardware_values = {}
            for key, title in (('gpu', 'Graphics'), ('vram', 'GPU memory'), ('ram', 'Available system memory'), ('placement', 'Model placement')):
                row = self.row()
                left = self.label(title, 'muted')
                left.set_hexpand(True)
                row.append(left)
                value = self.label('Checking…', 'value')
                row.append(value)
                self.hardware_values[key] = value
                hardware.append(row)
            box.append(hardware)
            box.append(self.section_heading('Provider access', self.button('View usage & limits →', lambda *_: self.stack.set_visible_child_name('usage'))))
            box.append(self.label('Cloud routes use provider limits. Local Qwen has no provider quota; speed depends on your hardware. Releasing the local model frees memory and it loads again when needed.', 'muted'))

        def render_models(self, status):
            if status.get('checking'):
                return
            self.model_metrics['cloud'][0].set_text('Installed' if status.get('programs', {}).get('agy') else 'Setup needed')
            self.model_metrics['free'][0].set_text('Check unavailable' if status.get('pricing_error') else str(len(status.get('free_models', []))) + ' verified models')
            self.model_metrics['local'][0].set_text(('GPU ready' if status.get('gpu') else 'CPU ready') if status.get('local_ready') else 'Setup needed')
            gpu, ram = status.get('gpu'), status.get('ram')
            self.hardware_values['gpu'].set_text(gpu['name'].replace('NVIDIA GeForce ', '') if gpu else 'CPU inference')
            self.hardware_values['vram'].set_text(f"{gpu['used_mb'] / 1024:.1f} / {gpu['total_mb'] / 1024:.0f} GB" if gpu else 'Not available')
            self.hardware_values['ram'].set_text(f"{ram['available_gb']} / {ram['total_gb']} GB" if ram else 'Not available')
            loaded = next((m for m in status.get('loaded', []) if m.get('name') == status.get('local_model') or m.get('model') == status.get('local_model')), None)
            placement = 'Sleeping · loads when needed'
            if loaded:
                placement = f"{round(loaded.get('size_vram', 0) / max(loaded.get('size', 1), 1) * 100)}% GPU · remainder on CPU" if loaded.get('size_vram') else 'CPU only'
            self.hardware_values['placement'].set_text(placement)
            self.release_button.set_sensitive(bool(loaded) and not any(t['status'] in dashboard.ACTIVE for t in self.tasks))

        def build_memory_page(self):
            box = self.page_box('Project memory', 'memory', 'Keep important requirements and project conventions across conversations.')
            self.memory_stats = self.label('Select a project in Workspace, then open this page.', 'muted')
            self.memory_stats.set_wrap(True)
            box.append(self.memory_stats)
            box.append(self.label('PINNED REQUIREMENTS', 'section'))
            note = self.label('Shared by every chat in this project. These requirements are kept verbatim in each task. Maximum 4,000 UTF-8 bytes.', 'muted')
            note.set_wrap(True)
            box.append(note)
            self.requirements = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            scroll = Gtk.ScrolledWindow(min_content_height=220)
            scroll.set_child(self.requirements)
            box.append(scroll)
            actions = self.row()
            self.save_memory_button = self.button('Save requirements', self.save_requirements, 'primary')
            actions.append(self.save_memory_button)
            actions.append(self.button('Reload', lambda *_: self.load_memory()))
            self.init_memory_button = self.button('Create instruction file', self.initialize_rules)
            actions.append(self.init_memory_button)
            box.append(actions)
            details = self.label('Source files are indexed locally before each task. Only relevant excerpts are included. Conversations keep checkpoints and retrieve earlier related turns. For a large initial scan, run:\n\ncodehub index --project /path/to/project', 'muted')
            details.set_wrap(True)
            details.set_selectable(True)
            box.append(details)

        def page_changed(self, *_):
            name = self.stack.get_visible_child_name()
            titles = {'workspace': 'Chats', 'history': 'Task history', 'models': 'Models & hardware', 'usage': 'Usage & limits', 'memory': 'Project memory'}
            self.page_title.set_text('Workspace / ' + titles.get(name, 'Chats'))
            for key, button in self.navigation.items():
                if key == name:
                    button.add_css_class('selected')
                else:
                    button.remove_css_class('selected')
            if hasattr(self, 'requirements') and self.stack.get_visible_child_name() == 'memory':
                self.load_memory()

        def load_memory(self):
            project = self.current_project()
            self.memory_project = project
            self.save_memory_button.set_sensitive(False)
            self.init_memory_button.set_sensitive(False)
            self.requirements.set_editable(False)
            self.memory_stats.set_text('Loading project memory…')
            def loaded(info):
                if project != self.memory_project:
                    return
                self.requirements.get_buffer().set_text(info['requirements'])
                files = ', '.join(item['name'] for item in info.get('guidance_files', [])) or 'No project instruction file yet'
                self.memory_stats.set_text(f"{Path(project).name} · Shared project memory\n{project}\n{info['indexed_files']} / {info['eligible_files']} source files indexed · {info['turns']} saved turns\n{files}")
                self.save_memory_button.set_sensitive(True)
                self.init_memory_button.set_sensitive(True)
                self.requirements.set_editable(True)
            self.background(lambda: api('project?path=' + quote(project)), loaded)

        def save_requirements(self, *_):
            if not self.memory_project or not self.save_memory_button.get_sensitive():
                return
            buffer = self.requirements.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            project = self.memory_project
            self.background(lambda: api('project/notes', {'project': project, 'requirements': text}),
                            lambda _: self.memory_stats.set_text('Saved for all chats in ' + project) if self.memory_project == project else None)

        def initialize_rules(self, *_):
            if not self.memory_project or not self.init_memory_button.get_sensitive():
                return
            project = self.memory_project
            self.background(lambda: api('project/init', {'project': project}), lambda _: self.load_memory() if self.memory_project == project else None)

        def build_quota_page(self):
            box = self.page_box('Usage & limits', 'usage', 'Provider allowances, reset times, and activity on this computer.')
            self.quota_refresh = self.button('Refresh quota', lambda *_: self.background(lambda: api('quota/refresh', {}), self.render_quota))
            box.append(self.section_heading('Antigravity', self.quota_refresh))
            self.quota_cards = self.flow(2)
            box.append(self.quota_cards)
            self.quota_checked = self.label('Checking official usage…', 'footnote')
            box.append(self.quota_checked)
            self.free_refresh = self.button('Refresh usage', lambda *_: self.background(lambda: api('free-quota/refresh', {}), self.render_free_quota))
            box.append(self.section_heading('OpenCode free models', self.free_refresh))
            reset = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            reset.add_css_class('notice')
            self.free_reset = self.label('Checking expected daily reset…', 'value')
            reset.append(self.free_reset)
            reset.append(self.label('Expected timing from published limiter code. Provider retry times take precedence.', 'muted'))
            box.append(reset)
            self.free_quota_cards = self.flow()
            box.append(self.free_quota_cards)
            self.free_quota_checked = self.label('', 'footnote')
            box.append(self.free_quota_checked)
            details = Gtk.Expander(label='How to read free-model usage')
            self.free_quota_intro = self.label('Reading local data…', 'muted')
            details.set_child(self.free_quota_intro)
            box.append(details)
            local = self.card()
            local.append(self.section_heading('Local Qwen 8B', self.label('No provider quota', 'pill')))
            local.append(self.label('No reset required. Speed and context depend on your hardware.', 'muted'))
            self.local_usage = self.label('Reading local usage…', 'value')
            local.append(self.local_usage)
            box.append(local)

        @staticmethod
        def reset_text(stamp, prefix='Resets'):
            if not stamp:
                return 'Reset time not reported'
            seconds = max(0, int(stamp - time.time()))
            return (prefix + ' ' + datetime.fromtimestamp(stamp).strftime('%d %b, %H:%M') +
                    f" · {seconds // 86400}d {seconds % 86400 // 3600}h {seconds % 3600 // 60}m")

        def render_free_quota(self, data):
            self.free_refresh.set_sensitive(not data.get('refreshing'))
            self.free_reset.set_text(self.reset_text(data.get('expected_reset_at'), 'Expected reset') + ' · 00:00 UTC')
            self.free_quota_intro.set_text('Remaining allowance is not reported by this provider. ' + data.get('coverage', '') + ' Counts use recorded UTC dates. Refreshing reads local data and sends no model prompts.')
            signature = json.dumps(data.get('models', []))
            if signature != self.free_signature:
                self.free_signature = signature
                expanded = {}
                child = self.free_quota_cards.get_first_child()
                while child:
                    card = child.get_child()
                    detail = card.get_last_child()
                    if isinstance(detail, Gtk.Expander):
                        expanded[card.get_name()] = detail.get_expanded()
                    child = child.get_next_sibling()
                self.clear_box(self.free_quota_cards)
                states = {'not_reported': 'Allowance unknown', 'rate_limited': 'Rate limit reported',
                          'limit_observed': 'Limit observed', 'retry_elapsed': 'Retry window passed'}
                for model in data.get('models', []):
                    card = self.card()
                    card.set_name(model['id'])
                    card.set_size_request(230, -1)
                    card.append(self.label(model['name'], 'section-title'))
                    state = self.label(states.get(model['status'], 'Unknown'), 'pill')
                    state.set_halign(Gtk.Align.START)
                    card.append(state)
                    usage = model.get('usage') or {}
                    for title, value in (('Remaining', 'Not reported'),
                                         ('Responses today', f"{usage['responses']:,}" if 'responses' in usage else 'Unavailable'),
                                         ('Tokens observed', f"{usage['total_tokens']:,}" if usage.get('total_tokens') is not None else 'Unavailable')):
                        row = self.row()
                        left = self.label(title, 'muted')
                        left.set_hexpand(True)
                        row.append(left)
                        row.append(self.label(value, 'value'))
                        card.append(row)
                    observation = model.get('observation')
                    if observation:
                        text = self.reset_text(observation['retry_at'], 'Provider retry') if observation.get('retry_at') else 'Provider retry time not reported.'
                        text += '\nObserved ' + datetime.fromtimestamp(observation['observed_at']).strftime('%d %b, %H:%M')
                        if model['status'] == 'retry_elapsed':
                            text += '\nAccess has not been rechecked.'
                        card.append(self.label(text, 'muted'))
                    details = Gtk.Expander(label='Token breakdown')
                    counts = usage.get('tokens')
                    text = '\n'.join(f"{label}: {counts[key]:,}" for key, label in (
                        ('input', 'Input'), ('output', 'Output'), ('reasoning', 'Reasoning'),
                        ('cache_read', 'Cache read'), ('cache_write', 'Cache write'))) if counts else 'Local usage unavailable.'
                    details.set_child(self.label(text, 'muted'))
                    details.set_expanded(expanded.get(model['id'], False))
                    card.append(details)
                    self.free_quota_cards.append(card)
            checked = data.get('checked_at')
            text = 'Local data checked ' + datetime.fromtimestamp(checked).strftime('%H:%M:%S') if checked else 'Checking local usage…'
            for key in ('error', 'clock_warning'):
                if data.get(key):
                    text += '\n' + data[key]
            self.free_quota_checked.set_text(text + (' · Refreshing…' if data.get('refreshing') else ''))
            usage = data.get('local', {}).get('usage')
            if usage:
                tokens = f"{usage['total_tokens']:,}" if usage.get('total_tokens') is not None else 'unavailable'
                self.local_usage.set_text(f"Today · {usage['responses']:,} responses · {tokens} observed tokens")
            else:
                self.local_usage.set_text('Local usage unavailable')

        def render_quota(self, data):
            self.quota_refresh.set_sensitive(not data.get('refreshing'))
            signature = json.dumps(data.get('groups', [])) + str(int(time.time() // 60))
            if signature != self.quota_signature:
                self.quota_signature = signature
                self.clear_box(self.quota_cards)
                if not data.get('groups'):
                    card = self.card()
                    card.append(self.label('Waiting for provider usage', 'section-title'))
                    card.append(self.label('Uses Antigravity’s official /usage command. Unknown limits stay unknown.', 'muted'))
                    self.quota_cards.append(card)
                for group in data.get('groups', []):
                    for bucket in group['buckets']:
                        card = self.card()
                        card.set_size_request(260, -1)
                        card.append(self.label(group['name'], 'section-title'))
                        card.append(self.label(f"{bucket['remaining_percent']:.1f}% remaining", 'quota-number'))
                        bar = Gtk.ProgressBar()
                        bar.set_fraction(max(0, min(1, bucket['remaining_percent'] / 100)))
                        card.append(bar)
                        card.append(self.label(self.reset_text(bucket.get('reset_at')), 'muted'))
                        self.quota_cards.append(card)
            checked = data.get('checked_at')
            text = 'Provider data checked ' + datetime.fromtimestamp(checked).strftime('%d %b, %H:%M:%S') if checked else 'Quota not reported yet.'
            if data.get('refreshing'):
                text += ' · Refreshing…'
            if data.get('stale'):
                text += ' · Cached / needs refresh'
            if data.get('error'):
                text += '\n' + data['error']
            self.quota_checked.set_text(text)

        def closed(self, window):
            self.save_draft()
            self.closed_window = True
            self.executor.shutdown(wait=False, cancel_futures=True)
            hub.save_json(hub.STATE / "desktop" / "window.json",
                          {"pid": os.getpid(), "status": "closed", "closed_at": time.time()})
            return False

    return CodingHub()


if __name__ == "__main__":
    sys.exit(launch())

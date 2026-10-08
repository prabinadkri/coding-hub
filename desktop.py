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
    routes = ("auto", "antigravity", "free", "local", "smart")

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
            self.history_updating = False
            self.refreshing = False
            self.connected = False
            self.last_output = None
            self.draft_path = hub.STATE / "desktop" / "draft.json"

        @staticmethod
        def row(spacing=12):
            return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)

        @staticmethod
        def label(text, style=None):
            label = Gtk.Label(label=text, xalign=0)
            if style:
                label.add_css_class(style)
            return label

        def button(self, text, callback, style=None):
            button = Gtk.Button(label=text)
            button.connect("clicked", callback)
            if style:
                button.add_css_class(style)
            return button

        def do_activate(self):
            if self.window:
                self.window.present()
                return
            Gtk.Window.set_default_icon_name("coding-hub")
            self.window = Gtk.ApplicationWindow(application=self, title="Coding Hub")
            self.window.set_default_size(1280, 880)
            self.window.set_icon_name("coding-hub")
            self.window.connect("close-request", self.closed)
            css = Gtk.CssProvider()
            css.load_from_data(b"""
                window { background: #f7f8fa; color: #19352b; }
                headerbar { background: #173f32; color: #ffffff; }
                headerbar button { color: #ffffff; background: #285642; }
                .heading { font-size: 23px; font-weight: 700; }
                .muted { color: #61746a; }
                .section { font-size: 11px; font-weight: 700; letter-spacing: 1px; }
                .status { padding: 12px; background: #e6eee2; border-radius: 10px; }
                .error { color: #a12d29; }
                .primary { background: #22553f; color: white; font-weight: 700; }
                .output text, .output { background: #152b23; color: #e4eee5; }
                textview { padding: 12px; border-radius: 8px; }
                button { padding: 9px 14px; border-radius: 8px; }
                entry { padding: 8px; border-radius: 8px; }
                .sidebar { background: #edf1ed; padding: 16px 12px; border-right: 1px solid #dce3dd; }
                .sidebar button { background: transparent; border: 0; padding: 9px; }
                .sidebar button:hover { background: #dfe8df; }
                .card { background: white; border: 1px solid #e1e6e1; border-radius: 12px; padding: 18px; }
                .user-message { background: #e6efe4; border-radius: 10px; padding: 14px; }
                .assistant-message { background: #ffffff; border: 1px solid #e0e7e0; border-radius: 10px; padding: 14px; }
                .chat-title { font-weight: 700; font-size: 12px; }
                .quota-number { font-size: 25px; font-weight: 700; }
            """)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            header = Gtk.HeaderBar()
            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            header.set_title_widget(Gtk.StackSwitcher(stack=self.stack))
            self.stack.connect("notify::visible-child-name", self.page_changed)
            header.pack_start(self.button("New chat", self.new_task))
            header.pack_end(self.button("Open web", lambda *_: webbrowser.open(url)))
            self.window.set_titlebar(header)
            keyboard = Gtk.EventControllerKey()
            keyboard.connect("key-pressed", self.shortcut)
            self.window.add_controller(keyboard)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
            for edge in ("top", "bottom", "start", "end"):
                getattr(body, "set_margin_" + edge)(22)
            shell = self.row(0)
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, width_request=235)
            sidebar.add_css_class("sidebar")
            sidebar.append(self.label("Coding Hub", "heading"))
            sidebar.append(self.label("PROJECTS & CHATS", "section"))
            self.project_tree = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            tree_scroll = Gtk.ScrolledWindow(vexpand=True)
            tree_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            tree_scroll.set_child(self.project_tree)
            sidebar.append(tree_scroll)
            sidebar.append(self.label("Local workspace · v2.1", "muted"))
            shell.append(sidebar)
            self.stack.set_hexpand(True)
            self.stack.set_vexpand(True)
            shell.append(self.stack)
            self.window.set_child(shell)
            work_scroll = Gtk.ScrolledWindow()
            work_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            work_scroll.set_child(body)
            self.stack.add_titled(work_scroll, "workspace", "Workspace")
            title = self.row()
            picture = Gtk.Image.new_from_file(str(hub.ROOT / "assets" / "icon.svg"))
            picture.set_pixel_size(48)
            title.append(picture)
            titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            self.chat_title = self.label("New conversation", "heading")
            self.chat_title.set_wrap(True)
            titles.append(self.chat_title)
            titles.append(self.label("Saved conversations, focused context, shared project memory.", "muted"))
            title.append(titles)
            body.append(title)
            self.hardware = self.label("Checking agents and hardware…", "status")
            self.hardware.set_wrap(True)
            body.append(self.hardware)
            body.append(self.label("PROJECT FOLDER", "section"))
            project_row = self.row()
            self.project = Gtk.Entry()
            self.project.set_hexpand(True)
            project_row.append(self.project)
            self.browse_button = self.button("Browse…", self.browse)
            project_row.append(self.browse_button)
            body.append(project_row)
            self.chat_messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            self.chat_scroll = Gtk.ScrolledWindow(min_content_height=130, max_content_height=300, propagate_natural_height=True)
            self.chat_scroll.set_child(self.chat_messages)
            self.chat_scroll.set_visible(False)
            body.append(self.chat_scroll)
            body.append(self.label("MESSAGE", "section"))
            self.prompt = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            prompt_scroll = Gtk.ScrolledWindow(min_content_height=125)
            prompt_scroll.set_child(self.prompt)
            body.append(prompt_scroll)
            options = self.row()
            self.route = Gtk.DropDown.new_from_strings(["Automatic", "Antigravity", "Free cloud", "Local Qwen", "Smart · Manager + worker"])
            self.route.connect("notify::selected", self.route_changed)
            self.quality = Gtk.DropDown.new_from_strings(["Fast", "Deep"])
            self.edits = Gtk.CheckButton(label="Allow edits & commands")
            options.append(self.route)
            options.append(self.quality)
            options.append(self.edits)
            spacer = Gtk.Box(hexpand=True)
            options.append(spacer)
            self.stop_button = self.button("Stop task", self.stop)
            self.stop_button.set_sensitive(False)
            self.run_button = self.button("Send", self.run_task, "primary")
            options.append(self.stop_button)
            options.append(self.run_button)
            body.append(options)
            self.hint = self.label("", "muted")
            self.hint.set_wrap(True)
            body.append(self.hint)
            self.route_changed()
            self.error = self.label("", "error")
            self.error.set_wrap(True)
            self.error.set_visible(False)
            body.append(self.error)
            history_row = self.row()
            history_row.append(self.label("RECENT TASKS", "section"))
            self.history = Gtk.DropDown.new_from_strings(["No tasks yet"])
            self.history.set_hexpand(True)
            self.history.connect("notify::selected", self.choose_history)
            history_row.append(self.history)
            history_row.append(self.button("Release model", self.unload))
            body.append(history_row)
            self.activity = self.label("Ready · Analysis mode reads project files. Enable edits to implement changes.", "muted")
            self.activity.set_wrap(True)
            body.append(self.activity)
            self.output = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.output.add_css_class("output")
            output_scroll = Gtk.ScrolledWindow(min_content_height=200, vexpand=True)
            output_scroll.set_child(self.output)
            self.output_details = Gtk.Expander(label="Live task output")
            self.output_details.set_child(output_scroll)
            body.append(self.output_details)
            body.append(self.label("Closing this window keeps tasks running. App and web share task history.", "muted"))
            self.build_memory_page()
            self.build_quota_page()
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
            return {"project": self.project.get_text(),
                    "prompt": buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
                    "backend": routes[self.route.get_selected()],
                    "quality": "deep" if self.quality.get_selected() == 1 else "fast",
                    "mode": "build" if self.edits.get_active() else "analysis", "conversation": self.conversation}

        def save_draft(self):
            if self.closed_window:
                return False
            hub.save_json(self.draft_path, self.form())
            return True

        def route_changed(self, *_):
            selected = self.route.get_selected()
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
                self.project.set_editable(False)
                self.browse_button.set_sensitive(False)
                self.prompt.get_buffer().set_text('')
                self.save_draft()
                self.output_details.set_expanded(True)
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

        def choose_history(self, *_):
            index = self.history.get_selected() - 1
            if self.history_updating or not 0 <= index < len(self.tasks):
                return
            self.selected = self.tasks[index]["id"]
            identifier = self.selected
            self.background(lambda: api("tasks/" + identifier), self.show_task)

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
                gpu_text = f"{gpu['name'].replace('NVIDIA GeForce ', '')} · {gpu['used_mb'] / 1024:.1f}/{gpu['total_mb'] / 1024:.0f} GB GPU" if gpu else "CPU inference"
                cloud = "Antigravity installed" if status.get("programs", {}).get("agy") else "Install Antigravity"
                models = len(status.get("free_models", []))
                local = "Local Qwen ready" if status.get("local_ready") else "Local model setup needed"
                self.hardware.set_text(f"{cloud}  •  {models} verified free models  •  {local}  •  {gpu_text}")
            signature = [(t["id"], t["status"]) for t in self.tasks]
            if signature != self.history_signature:
                self.history_signature = signature
                self.history_updating = True
                self.history.set_model(Gtk.StringList.new(["Choose a recent task…"] + [f"{t['status'].capitalize()} · {t['prompt'][:75].replace(chr(10), ' ')}" for t in self.tasks]))
                index = next((i + 1 for i, t in enumerate(self.tasks) if t["id"] == self.selected), 0)
                self.history.set_selected(index)
                self.history_updating = False
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
            self.clear_box(self.project_tree)
            if not projects:
                note = self.label('Start a chat to add a project.', 'muted')
                note.set_wrap(True)
                self.project_tree.append(note)
            for project in projects:
                group = Gtk.Expander(label=project['name'])
                group.set_tooltip_text(project['project'])
                group.set_expanded(project['project'] == self.conversation_project or len(projects) == 1)
                chats = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                chats.set_margin_start(12)
                for chat in project['conversations']:
                    title = chat['goal'].replace('\n', ' ')
                    button = self.button(title[:25] + ('…' if len(title) > 25 else ''),
                        lambda _, p=project['project'], c=chat['id']: self.open_chat(p, c))
                    button.set_tooltip_text(title)
                    if chat['id'] == self.conversation:
                        button.add_css_class('suggested-action')
                    chats.append(button)
                def new_in_project(_, path=project['project']):
                    self.new_task()
                    self.project.set_text(path)
                    self.save_draft()
                chats.append(self.button('+ New chat', new_in_project))
                group.set_child(chats)
                self.project_tree.append(group)

        def open_chat(self, project, identifier):
            def opened(data):
                self.conversation = identifier
                self.conversation_project = project
                self.selected = None
                self.project.set_text(project)
                self.project.set_editable(False)
                self.browse_button.set_sensitive(False)
                self.prompt.get_buffer().set_text('')
                self.message_signature = None
                self.render_messages(data)
                self.stack.set_visible_child_name('workspace')
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
            self.chat_title.set_text(data['goal'].split('.')[0].split('\n')[0][:70])
            self.chat_scroll.set_visible(True)
            self.clear_box(self.chat_messages)
            def add(role, text, status=''):
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                card.add_css_class('user-message' if role == 'You' else 'assistant-message')
                card.append(self.label(role + (' · ' + status if status else ''), 'chat-title'))
                content = self.label(text)
                content.set_wrap(True)
                content.set_selectable(True)
                content.set_max_width_chars(75)
                card.append(content)
                self.chat_messages.append(card)
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

        def page_box(self, title, name):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            for edge in ('top', 'bottom', 'start', 'end'):
                getattr(box, 'set_margin_' + edge)(26)
            box.append(self.label(title, 'heading'))
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_child(box)
            self.stack.add_titled(scroll, name, title)
            return box

        def build_memory_page(self):
            box = self.page_box('Project memory', 'memory')
            self.memory_stats = self.label('Select a project in Workspace, then open this page.', 'muted')
            self.memory_stats.set_wrap(True)
            box.append(self.memory_stats)
            box.append(self.label('PINNED REQUIREMENTS', 'section'))
            note = self.label('These requirements are kept verbatim in every task for this project. Maximum 4,000 UTF-8 bytes.', 'muted')
            note.set_wrap(True)
            box.append(note)
            self.requirements = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            scroll = Gtk.ScrolledWindow(min_content_height=220)
            scroll.set_child(self.requirements)
            box.append(scroll)
            box.append(self.button('Save requirements', self.save_requirements, 'primary'))
            box.append(self.button('Reload project memory', lambda *_: self.load_memory()))
            box.append(self.button('Create CODING_HUB.md', self.initialize_rules))
            details = self.label('Source files are indexed locally before each task. Only relevant excerpts are included. Conversations keep checkpoints and retrieve earlier related turns. For a large initial scan, run:\n\ncodehub index --project /path/to/project', 'muted')
            details.set_wrap(True)
            details.set_selectable(True)
            box.append(details)

        def page_changed(self, *_):
            if hasattr(self, 'requirements') and self.stack.get_visible_child_name() == 'memory':
                self.load_memory()

        def load_memory(self):
            project = self.project.get_text()
            def loaded(info):
                self.requirements.get_buffer().set_text(info['requirements'])
                files = ', '.join(item['name'] for item in info.get('guidance_files', [])) or 'No project instruction file yet'
                self.memory_stats.set_text(f"{project}\n{info['indexed_files']} / {info['eligible_files']} source files indexed · {info['turns']} saved turns\n{files}")
            self.background(lambda: api('project?path=' + quote(project)), loaded)

        def save_requirements(self, *_):
            buffer = self.requirements.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            project = self.project.get_text()
            self.background(lambda: api('project/notes', {'project': project, 'requirements': text}),
                            lambda _: self.memory_stats.set_text('Saved. Requirements will be retained in each task for ' + project))

        def initialize_rules(self, *_):
            project = self.project.get_text()
            self.background(lambda: api('project/init', {'project': project}), lambda _: self.load_memory())

        def build_quota_page(self):
            box = self.page_box('Usage', 'usage')
            box.append(self.label('OpenCode free models', 'chat-title'))
            self.free_quota_intro = self.label('Reading local usage…', 'muted')
            self.free_quota_intro.set_wrap(True)
            box.append(self.free_quota_intro)
            self.free_quota_cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.append(self.free_quota_cards)
            self.free_quota_checked = self.label('', 'muted')
            self.free_quota_checked.set_wrap(True)
            box.append(self.free_quota_checked)
            box.append(self.button('Refresh free-model usage', lambda *_: self.background(lambda: api('free-quota/refresh', {}), self.render_free_quota)))
            box.append(self.label('Antigravity', 'chat-title'))
            note = self.label('Live Antigravity limits from its official /usage command. Groups share the displayed allowance.', 'muted')
            note.set_wrap(True)
            box.append(note)
            self.quota_cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
            box.append(self.quota_cards)
            self.quota_checked = self.label('Checking quota…', 'muted')
            self.quota_checked.set_wrap(True)
            box.append(self.quota_checked)
            box.append(self.button('Refresh quota', lambda *_: self.background(lambda: api('quota/refresh', {}), self.render_quota), 'primary'))
            box.append(self.label('AI credit overages are disabled by setup. Free provider quotas can change.', 'muted'))

        def render_free_quota(self, data):
            self.clear_box(self.free_quota_cards)
            text = 'Remaining allowance is not reported here. ' + data.get('coverage', 'Reading local OpenCode data…')
            if data.get('expected_reset_at'):
                seconds = max(0, int(data['expected_reset_at'] - time.time()))
                text += '\nExpected daily reset: ' + datetime.fromtimestamp(data['expected_reset_at']).strftime('%d %b, %H:%M')
                text += f' · {seconds // 3600}h {seconds % 3600 // 60}m (00:00 UTC). Based on published limiter code, not a live guarantee.'
            self.free_quota_intro.set_text(text)
            states = {'not_reported': 'Allowance unknown', 'rate_limited': 'Rate limit reported',
                      'limit_observed': 'Limit observed', 'retry_elapsed': 'Retry window passed; access not rechecked'}
            for model in data.get('models', []):
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                card.add_css_class('card')
                card.append(self.label(model['name'], 'chat-title'))
                line = self.label(states.get(model['status'], 'Unknown'), 'muted')
                line.set_wrap(True)
                card.append(line)
                card.append(self.label('Remaining: not reported', 'muted'))
                usage = model.get('usage')
                if usage:
                    tokens = f"{usage['total_tokens']:,}" if usage.get('total_tokens') is not None else 'unavailable'
                    card.append(self.label(f"Today: {usage['responses']:,} AI responses · {tokens} observed tokens", 'muted'))
                    counts = usage['tokens']
                    detail = self.label(f"Input {counts['input']:,} · Output {counts['output']:,} · Reasoning {counts['reasoning']:,}\nCache read/write {counts['cache_read']:,} / {counts['cache_write']:,}", 'muted')
                    detail.set_wrap(True)
                    card.append(detail)
                else:
                    card.append(self.label('Local usage unavailable', 'muted'))
                observation = model.get('observation')
                if observation:
                    retry = observation.get('retry_at')
                    label = 'Provider retry time: ' + datetime.fromtimestamp(retry).strftime('%d %b, %H:%M:%S') if retry else 'Provider retry time not reported.'
                    label += '\nObserved ' + datetime.fromtimestamp(observation['observed_at']).strftime('%d %b, %H:%M:%S')
                    line = self.label(label, 'muted')
                    line.set_wrap(True)
                    card.append(line)
                self.free_quota_cards.append(card)
            local = self.label('Local Qwen: no provider quota and no reset required. Hardware and context limits still apply.', 'muted')
            local.set_wrap(True)
            self.free_quota_cards.append(local)
            usage = data.get('local', {}).get('usage')
            if usage:
                tokens = f"{usage['total_tokens']:,}" if usage.get('total_tokens') is not None else 'unavailable'
                self.free_quota_cards.append(self.label(f"Today: {usage['responses']:,} AI responses · {tokens} observed tokens", 'muted'))
            checked = data.get('checked_at')
            text = 'Local data checked ' + datetime.fromtimestamp(checked).strftime('%H:%M:%S') if checked else 'Checking local usage…'
            if data.get('refreshing'):
                text += ' · Refreshing…'
            if data.get('error'):
                text += '\n' + data['error']
            if data.get('clock_warning'):
                text += '\n' + data['clock_warning']
            self.free_quota_checked.set_text(text)

        def render_quota(self, data):
            self.clear_box(self.quota_cards)
            for group in data.get('groups', []):
                for bucket in group['buckets']:
                    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                    card.add_css_class('card')
                    card.append(self.label(group['name'], 'chat-title'))
                    card.append(self.label(f"{bucket['remaining_percent']:.1f}% remaining", 'quota-number'))
                    bar = Gtk.ProgressBar()
                    bar.set_fraction(bucket['remaining_percent'] / 100)
                    card.append(bar)
                    reset = bucket.get('reset_at')
                    if reset:
                        seconds = max(0, int(reset - time.time()))
                        text = 'Resets ' + datetime.fromtimestamp(reset).strftime('%d %b %Y, %H:%M') + f" · {seconds // 86400}d {seconds % 86400 // 3600}h {seconds % 3600 // 60}m"
                    else:
                        text = 'Reset time not reported'
                    card.append(self.label(text, 'muted'))
                    self.quota_cards.append(card)
            checked = data.get('checked_at')
            text = 'Checked ' + datetime.fromtimestamp(checked).strftime('%d %b, %H:%M:%S') if checked else 'Quota not reported yet.'
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

    application = CodingHub()
    def quit_for_update():
        if application.window and not application.closed_window:
            application.closed(application.window)
        application.quit()
        return False
    signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(quit_for_update))
    return application.run([])


if __name__ == "__main__":
    sys.exit(launch())

"""A native Linux application window sharing the local Coding Hub dashboard."""
from __future__ import annotations

import concurrent.futures
import json
import os
import signal
import traceback
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
import subprocess
import sys
import time
import urllib.error
import urllib.request

import dashboard
import hub
from message_format import blocks, inline_markup, clean_reply


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


def prepare_browser_environment():
    """Include desktop registrations omitted by minimal SSH launch environments."""
    existing = os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':')
    candidates = [Path.home()/'.local/share/flatpak/exports/share',
                  Path('/var/lib/flatpak/exports/share'), Path('/var/lib/snapd/desktop')]
    additions = [str(path) for path in candidates if path.is_dir() and str(path) not in existing]
    os.environ['XDG_DATA_DIRS'] = ':'.join(existing + additions)


def launch_browser(Gtk, Gdk, Gio, parent, url, finished):
    """Launch through the desktop session and report asynchronous failure."""
    if hasattr(Gtk, 'UriLauncher'):
        launcher = Gtk.UriLauncher.new(url)
        def complete(source, result, *_):
            try: finished(bool(source.launch_finish(result)), None)
            except Exception as error: finished(False, str(error))
        launcher.launch(parent, None, complete)
    else:
        context = Gdk.Display.get_default().get_app_launch_context()
        def complete(source, result, *_):
            try: finished(bool(Gio.AppInfo.launch_default_for_uri_finish(result)), None)
            except Exception as error: finished(False, str(error))
        Gio.AppInfo.launch_default_for_uri_async(url, context, None, complete)


def launch(port=8765):
    if not sys.platform.startswith("linux"):
        print("The native app requires Linux. Opening the browser interface on this platform.")
        return dashboard.serve(port)
    prepare_browser_environment()
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


def create_application(Gtk, Gdk, Gio, GLib, api, url, browser_launcher=None):
    """Build native widgets independently of transport for isolated UI validation."""
    from gi.repository import Pango
    routes = ("auto", "antigravity", "free", "local", "smart", "claude", "openai")

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
            self.pending_indicator = None
            self.tasks = []
            self.history_signature = None
            self.refreshing = False
            self.history_epoch = 0
            self.deleting_chat = False
            self.connected = False
            self.memory_project = None
            self.chat_layout = None
            self.follow_chat = True
            self.scroll_pending = False
            self.model_choices = {}
            self.chosen_models = {}
            self.model_signature = None
            self.signin_id = None
            self.signin_busy = False
            self.last_output = None
            self.quota_signature = None
            self.free_signature = None
            self.draft_path = hub.STATE / "desktop" / "draft.json"
            self.preferences_path = hub.STATE / 'desktop' / 'preferences.json'
            self.sidebar_save_source = None

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
            Gtk.Window.set_default_icon_name('io.github.prabinadkri.CodingHub')
            self.window = Gtk.ApplicationWindow(application=self, title='Coding Hub')
            self.window.set_default_size(1240, 840)
            self.window.set_icon_name('io.github.prabinadkri.CodingHub')
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
            self.open_web_button = self.button('Open web ↗', self.open_web)
            header.pack_end(self.open_web_button)
            self.theme_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.theme_switch.set_tooltip_text('Dark mode')
            theme_row = self.row(7)
            theme_label = self.label('Dark', 'muted')
            theme_label.set_wrap(False)
            theme_row.append(theme_label)
            theme_row.append(self.theme_switch)
            header.pack_end(theme_row)
            self.theme_switch.connect('notify::active', self.theme_changed)
            try:
                dark = bool(json.loads(self.preferences_path.read_text()).get('dark', False))
            except (OSError, ValueError, TypeError):
                dark = False
            self.theme_switch.set_active(dark)
            self.theme_changed()
            self.window.set_titlebar(header)
            keyboard = Gtk.EventControllerKey()
            keyboard.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            self.keyboard = keyboard
            keyboard.connect('key-pressed', self.shortcut)
            self.window.add_controller(keyboard)
            shell = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            shell.add_css_class('workspace-split')
            shell.set_resize_start_child(False)
            shell.set_shrink_start_child(False)
            shell.set_resize_end_child(True)
            shell.set_shrink_end_child(False)
            self.workspace_split = shell
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=200)
            self.sidebar = sidebar
            sidebar.add_css_class('sidebar')
            brand = self.row(10)
            picture = Gtk.Image.new_from_file(str(hub.ROOT / 'assets' / 'icon.svg'))
            picture.set_pixel_size(36)
            brand.append(picture)
            brand.append(self.label('Coding Hub', 'brand'))
            sidebar.append(brand)
            sidebar.append(self.label('YOUR CODING WORKSPACE', 'brand-caption'))
            new = self.button('+  New chat', self.new_task, 'new-chat')
            new.set_margin_top(10)
            new.set_margin_bottom(0)
            new.set_tooltip_text('New conversation · Ctrl+N')
            sidebar.append(new)
            sidebar.append(self.button('+  New project', self.new_project, 'new-project'))
            tools_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                   hhomogeneous=False, vhomogeneous=False, hexpand=True, vexpand=True)
            self.navigation = {}
            for name, title, icon in (
                ('workspace', 'Chats', 'user-available-symbolic'),
                ('history', 'Task history', 'document-open-recent-symbolic'),
                ('models', 'Models & hardware', 'computer-symbolic'),
                ('usage', 'Usage & limits', 'view-statistics-symbolic'),
                ('accounts', 'Accounts', 'avatar-default-symbolic'),
                ('memory', 'Project memory', 'accessories-text-editor-symbolic')):
                button = self.button('', lambda _, page=name: self.stack.set_visible_child_name(page), 'nav-item')
                row = self.row(10)
                row.append(Gtk.Image.new_from_icon_name(icon))
                row.append(self.label(title))
                button.set_child(row)
                self.navigation[name] = button
                (sidebar if name == 'workspace' else tools_box).append(button)
            project_heading = self.label('PROJECTS & CHATS', 'section')
            project_heading.set_margin_top(6)
            sidebar.append(project_heading)
            self.project_tree = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            tree_scroll = Gtk.ScrolledWindow(vexpand=True)
            tree_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            tree_scroll.set_child(self.project_tree)
            sidebar.append(tree_scroll)
            self.sidebar_tree_scroll = tree_scroll
            self.sidebar_tools = Gtk.Expander(label='Tools & settings', expanded=False)
            self.sidebar_tools.set_child(tools_box)
            sidebar.append(self.sidebar_tools)
            sidebar.append(self.label('Local workspace', 'sidebar-foot'))
            sidebar.append(self.label('Version ' + dashboard.VERSION, 'sidebar-foot'))
            shell.set_start_child(sidebar)
            main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.error = self.label('', 'error-banner')
            self.error.set_visible(False)
            main.append(self.error)
            main.append(self.stack)
            shell.set_end_child(main)
            try:
                width = int(json.loads(self.preferences_path.read_text()).get('sidebar_width', 248))
            except (OSError, ValueError, TypeError):
                width = 248
            shell.set_position(max(220, min(420, width)))
            shell.connect('notify::position', self.sidebar_resized)
            self.window.set_child(shell)
            self.stack.connect('notify::visible-child-name', self.page_changed)
            body = self.page_box('New conversation', 'workspace', 'Choose a project and start a focused conversation.')
            self.chat_title = self.page_headings['workspace']
            self.chat_title.set_wrap(False)
            self.chat_title.set_ellipsize(Pango.EllipsizeMode.END)
            self.chat_subtitle = self.page_subtitles['workspace']
            self.workspace_body = body
            body.set_spacing(10)
            self.chat_memory_button = self.button('Project memory', lambda *_: self.stack.set_visible_child_name('memory'))
            self.page_header_rows['workspace'].append(self.chat_memory_button)
            self.scope_row = self.row()
            scope_label = self.label('Task type', 'muted')
            scope_label.set_wrap(False)
            self.scope_row.append(scope_label)
            self.scope = Gtk.DropDown.new_from_strings(['General task', 'Project task'])
            self.scope.set_selected(0)
            self.scope.connect('notify::selected', lambda *_: self.sync_chat_layout() if hasattr(self, 'composer_title') else None)
            self.scope_row.append(self.scope)
            body.append(self.scope_row)
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
            self.chat_messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            self.chat_scroll = Gtk.ScrolledWindow(min_content_height=120, vexpand=True)
            self.chat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.chat_scroll.set_child(self.chat_messages)
            self.chat_scroll.set_visible(False)
            adjustment = self.chat_scroll.get_vadjustment()
            adjustment.connect('value-changed', self.chat_scrolled)
            adjustment.connect('changed', self.chat_resized)
            body.append(self.chat_scroll)
            self.latest_button = self.button('↓  Latest messages', self.jump_to_latest)
            self.latest_button.set_halign(Gtk.Align.CENTER)
            self.latest_button.set_visible(False)
            body.append(self.latest_button)
            composer = self.card(8)
            composer.add_css_class('composer')
            self.composer = composer
            self.composer_title = self.label('Your message', 'section-title')
            composer_heading = self.row()
            self.composer_heading = composer_heading
            self.composer_title.set_hexpand(True)
            composer_heading.append(self.composer_title)
            composer_heading.append(self.label('Ctrl+Enter to send', 'muted'))
            composer.append(composer_heading)
            self.prompt = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.prompt.set_tooltip_text('Describe what you want to build, fix, or understand')
            self.prompt_scroll = Gtk.ScrolledWindow(min_content_height=52, max_content_height=120,
                                                   propagate_natural_height=True)
            self.prompt_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            prompt_scroll = self.prompt_scroll
            prompt_scroll.add_css_class('input-frame')
            prompt_scroll.set_child(self.prompt)
            overlay = Gtk.Overlay()
            overlay.set_child(prompt_scroll)
            self.prompt_placeholder = self.label('Describe what you want to build, fix, or understand…', 'prompt-placeholder')
            self.prompt_placeholder.set_halign(Gtk.Align.START)
            self.prompt_placeholder.set_valign(Gtk.Align.START)
            self.prompt_placeholder.set_margin_start(13)
            self.prompt_placeholder.set_margin_top(12)
            self.prompt_placeholder.set_can_target(False)
            overlay.add_overlay(self.prompt_placeholder)
            self.prompt.get_buffer().connect('changed', lambda buffer: self.prompt_placeholder.set_visible(buffer.get_char_count() == 0))
            composer.append(overlay)
            options = self.row()
            route_label = self.label('Route', 'muted')
            route_label.set_wrap(False)
            options.append(route_label)
            self.route = Gtk.DropDown.new_from_strings(['Automatic', 'Antigravity', 'Free cloud', 'Local Qwen', 'Smart', 'Claude account', 'ChatGPT account'])
            self.route.set_tooltip_text('Coding route')
            self.route.connect('notify::selected', self.route_changed)
            self.quality = Gtk.DropDown.new_from_strings(['Fast', 'Deep'])
            self.quality.set_tooltip_text('Antigravity quality')
            options.append(self.route)
            options.append(self.quality)
            settings_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            settings_body.append(options)
            self.model_row = self.row()
            model_label = self.label('Model', 'muted')
            model_label.set_wrap(False)
            self.model_row.append(model_label)
            self.model_picker = Gtk.DropDown.new_from_strings(['Use route default'])
            self.model_picker.set_hexpand(True)
            self.model_picker.connect('notify::selected', self.model_changed)
            self.model_row.append(self.model_picker)
            settings_body.append(self.model_row)
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
            self.stop_button.set_visible(False)
            self.run_button = self.button('Send  ↑', self.run_task, 'primary')
            footer.append(self.stop_button)
            footer.append(self.run_button)
            composer.append(footer)
            body.append(composer)
            self.activity = self.label('Ready · Analysis mode reads project files. Enable edits to implement changes.', 'muted')
            body.append(self.activity)
            self.output = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.output.add_css_class('output')
            output_scroll = Gtk.ScrolledWindow(min_content_height=100, max_content_height=160)
            output_scroll.set_child(self.output)
            self.output_details = Gtk.Expander(label='Live task output')
            self.output_details.set_child(output_scroll)
            body.append(self.output_details)
            self.chat_footnote = self.label('App and web share your chats. Closing this window keeps tasks running.', 'footnote')
            body.append(self.chat_footnote)
            self.build_history_page()
            self.build_models_page()
            self.build_accounts_page()
            self.build_memory_page()
            self.build_quota_page()
            self.stack.set_visible_child_name('workspace')
            self.page_changed()
            try:
                draft = json.loads(self.draft_path.read_text())
                self.project.set_text(draft.get("project", str(Path.home() / "Documents")))
                self.scope.set_selected(0 if draft.get("scope") == "general" or not draft else 1)
                self.conversation = draft.get("conversation")
                self.conversation_project = self.project.get_text() if self.conversation else ''
                self.project.set_editable(not bool(self.conversation))
                self.browse_button.set_sensitive(not bool(self.conversation))
                self.prompt.get_buffer().set_text(draft.get("prompt", ""))
                self.route.set_selected(routes.index(draft.get("backend", "auto")))
                if draft.get('model'):
                    self.chosen_models[draft.get('backend')] = draft['model']
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
            GLib.timeout_add(600, self.poll_signin)

        def background(self, work, callback, on_error=None):
            future = self.executor.submit(work)
            def done(result):
                def finish():
                    if self.closed_window:
                        return False
                    try:
                        callback(result.result())
                    except Exception as error:
                        print("App request failed: " + str(error), file=sys.stderr, flush=True)
                        traceback.print_exc()
                        if on_error:
                            on_error(error)
                            return False
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
                if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                    if self.stack.get_visible_child_name() == 'workspace' and self.window.get_focus() == self.prompt:
                        if self.run_button.get_sensitive():
                            self.run_task()
                        return True
                if key in (Gdk.KEY_n, Gdk.KEY_N):
                    self.new_task()
                    return True
            return False

        def theme_changed(self, *_):
            dark = self.theme_switch.get_active()
            Gtk.Settings.get_default().set_property('gtk-theme-name', 'Adwaita')
            Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', dark)
            if dark:
                self.window.add_css_class('dark')
            else:
                self.window.remove_css_class('dark')
            self.save_preferences(dark=dark)

        def save_preferences(self, **updates):
            try:
                preferences = json.loads(self.preferences_path.read_text())
                if not isinstance(preferences, dict):
                    preferences = {}
            except (OSError, ValueError):
                preferences = {}
            preferences.update(updates)
            hub.save_json(self.preferences_path, preferences)

        def sidebar_resized(self, *_):
            width = self.workspace_split.get_position()
            bounded = max(220, min(420, width))
            if width != bounded:
                self.workspace_split.set_position(bounded)
                return
            if self.sidebar_save_source:
                GLib.source_remove(self.sidebar_save_source)
            def save():
                self.sidebar_save_source = None
                self.save_preferences(sidebar_width=self.workspace_split.get_position())
                return False
            self.sidebar_save_source = GLib.timeout_add(300, save)

        def chat_scrolled(self, adjustment):
            if not self.scroll_pending:
                self.follow_chat = adjustment.get_upper() - adjustment.get_page_size() - adjustment.get_value() < 48
                self.latest_button.set_visible(not self.follow_chat and bool(self.conversation))

        def chat_resized(self, *_):
            if self.follow_chat:
                self.queue_chat_scroll()

        def queue_chat_scroll(self):
            if self.scroll_pending:
                return
            self.scroll_pending = True
            # Run after GTK has allocated the updated message widgets.
            def scroll():
                adjustment = self.chat_scroll.get_vadjustment()
                adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
                self.scroll_pending = False
                self.latest_button.set_visible(False)
                return False
            GLib.idle_add(scroll, priority=GLib.PRIORITY_LOW)

        def jump_to_latest(self, *_):
            self.follow_chat = True
            self.queue_chat_scroll()

        def form(self):
            buffer = self.prompt.get_buffer()
            return {"project": self.current_project(), 'scope': 'general' if self.scope.get_selected() == 0 else 'project',
                    "prompt": buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
                    "backend": routes[self.route.get_selected()],
                    "quality": "deep" if self.quality.get_selected() == 1 else "fast",
                    "model": self.chosen_models.get(routes[self.route.get_selected()]) if routes[self.route.get_selected()] in ('antigravity', 'claude', 'openai', 'free', 'local') else None,
                    "mode": "build" if self.edits.get_active() else "analysis", "conversation": self.conversation}

        def current_project(self):
            return self.conversation_project if self.conversation else self.project.get_text()

        def sync_chat_layout(self):
            existing = bool(self.conversation)
            general = self.scope.get_selected() == 0
            self.scope_row.set_visible(not existing)
            self.project_card.set_visible(not existing and not general)
            self.chat_memory_button.set_visible(existing or not general)
            self.chat_memory_button.set_label('Memory' if general else 'Project memory')
            self.edits.set_label('Allow commands & changes' if general else 'Allow edits & commands')
            self.project.set_editable(not existing)
            self.browse_button.set_sensitive(not existing)
            self.composer_title.set_text('Reply' if existing else 'Your message')
            self.composer_heading.set_visible(not existing)
            self.prompt_placeholder.set_text('Continue the conversation…' if existing else 'Describe what you want to build, fix, or understand…')
            self.activity.set_visible(not existing)
            self.prompt_scroll.set_min_content_height(48 if existing else 72)
            self.chat_footnote.set_visible(not existing)
            if self.chat_layout != existing:
                self.chat_settings.set_expanded(not existing)
                self.output_details.set_expanded(False)
                self.chat_layout = existing
            if existing:
                self.workspace_body.add_css_class('conversation-page')
                self.chat_subtitle.set_text(('General task' if general else Path(self.conversation_project).name) + ' · Saved conversation')
                self.chat_subtitle.set_tooltip_text(None if general else self.conversation_project)
            else:
                self.workspace_body.remove_css_class('conversation-page')
                self.chat_subtitle.set_text('Ask a question, diagnose Linux, or describe a standalone task.' if general else 'Choose a project and start a focused conversation.')
                self.chat_subtitle.set_tooltip_text(None)

        def save_draft(self):
            if self.closed_window:
                return False
            hub.save_json(self.draft_path, self.form())
            return True

        def route_changed(self, *_):
            selected = self.route.get_selected()
            if hasattr(self, 'chat_settings'):
                self.chat_settings.set_label('Chat settings · ' + ('Automatic', 'Antigravity', 'Free cloud', 'Local Qwen', 'Smart', 'Claude account', 'ChatGPT account')[selected])
            if hasattr(self, "quality"):
                self.quality.set_sensitive(routes[selected] in ("auto", "antigravity", "smart"))
            if hasattr(self, "hint"):
                self.hint.set_text([
                    "Antigravity → verified free models → local Qwen. Continues from partial work if a provider fails.",
                    "Uses your Google sign-in. Fast selects Flash; Deep selects Pro. Provider quotas apply.",
                    "Checks current zero-cost pricing before each run. Provider quotas apply.",
                    "Qwen3 8B runs on this computer with 16K context. Best for focused tasks.",
                    "Antigravity plans and reviews; free/local workers implement. At most 2 manager calls. Savings and equal quality are not guaranteed; 6,000-byte request limit.",
                    "Uses your separate Claude subscription through its official CLI. Connect in Accounts first; your plan's limits apply.",
                    "Uses your separate ChatGPT subscription through OpenCode. Connect in Accounts and choose a model; availability depends on your plan."
                ][selected])
            if hasattr(self, 'model_row'):
                self.render_model_picker()

        def render_model_picker(self):
            route = routes[self.route.get_selected()]
            choices = self.model_choices.get(route, [])
            selected = self.chosen_models.get(route)
            if selected and not any(m['id'] == selected for m in choices):
                choices = [{'id': selected, 'name': selected}] + choices
            keys = [None] + [m['id'] for m in choices]
            signature = (route, keys)
            self.model_row.set_visible(route in ('antigravity', 'claude', 'openai', 'free', 'local'))
            if signature == self.model_signature:
                return
            self.model_signature = signature
            self.model_keys = keys
            self.setting_models = True
            self.model_picker.set_model(Gtk.StringList.new(['Choose a model' if route == 'openai' else 'Use route default'] + [m['name'] for m in choices]))
            self.model_picker.set_selected(keys.index(selected) if selected in keys else 0)
            self.setting_models = False

        def model_changed(self, *_):
            if getattr(self, 'setting_models', True):
                return
            selected = self.model_picker.get_selected()
            if selected < len(self.model_keys):
                self.chosen_models[routes[self.route.get_selected()]] = self.model_keys[selected]

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

        def new_project(self, *_):
            self.new_task()
            self.scope.set_selected(1)
            self.project.set_text('')
            self.save_draft()
            self.browse()

        def new_task(self, *_):
            self.follow_chat = True
            self.latest_button.set_visible(False)
            self.conversation = None
            self.scope.set_selected(0)
            self.conversation_project = ''
            self.message_archive = {}
            self.archive_conversation = None
            self.pending_indicator = None
            self.message_signature = None
            self.chat_scroll.set_visible(False)
            self.chat_title.set_text("New conversation")
            self.chat_title.set_tooltip_text(None)
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
            self.output_details.set_label('Task activity')
            self.stop_button.set_sensitive(False)
            self.stop_button.set_visible(False)
            self.save_draft()
            self.prompt.grab_focus()

        def run_task(self, *_):
            data = self.form()
            if not data['prompt'].strip():
                self.prompt.grab_focus()
                return
            self.save_draft()
            self.error.set_visible(False)
            self.run_button.set_sensitive(False)
            def started(task):
                self.follow_chat = True
                self.tasks = [task] + [t for t in self.tasks if t['id'] != task['id']]
                self.selected = task["id"]
                self.conversation = task['conversation']
                self.conversation_project = task['project']
                self.scope.set_selected(0 if task.get('scope') == 'general' else 1)
                self.sync_chat_layout()
                self.project.set_editable(False)
                self.browse_button.set_sensitive(False)
                self.prompt.get_buffer().set_text('')
                self.save_draft()
                self.output_details.set_expanded(False)
                self.show_task(task)
                self.message_signature = None
                self.render_messages({'id': self.conversation, 'goal': task['prompt'], 'turns': [],
                                      'total_turns': len(self.message_archive)})
                self.prompt.grab_focus()
                self.refresh()
            self.background(lambda: api("tasks", data), started)

        def stop(self, *_):
            if self.selected:
                identifier = self.selected
                self.stop_button.set_sensitive(False)
                self.background(lambda: api("stop", {"id": identifier}), self.show_task)

        def unload(self, *_):
            self.background(lambda: api("unload", {"model": getattr(self, "loaded_model", hub.LOCAL_AGENT_MODEL)}), lambda _: self.refresh())

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
            self.stop_button.set_visible(task['status'] in dashboard.ACTIVE)
            self.output_details.set_label('Task activity · ' + task['status'].replace('_', ' ').capitalize() + ' · ' + str(elapsed // 60) + 'm ' + str(elapsed % 60) + 's')
            output = task.get("output", "")
            if output != self.last_output:
                self.last_output = output
                buffer = self.output.get_buffer()
                buffer.set_text(output or "Waiting for the agent’s first update…")
                self.output.scroll_to_iter(buffer.get_end_iter(), 0, False, 0, 1)

        def refresh(self):
            if self.closed_window:
                return False
            if self.refreshing or self.deleting_chat:
                return True
            self.refreshing = True
            epoch = self.history_epoch
            selected = self.selected
            conversation, project = self.conversation, self.conversation_project
            def fetch():
                status = api("status")
                tasks = api("tasks")["tasks"]
                identifier = selected or next((t["id"] for t in tasks if conversation and t.get('conversation') == conversation and t["status"] in dashboard.ACTIVE), None)
                detail = api("tasks/" + identifier) if identifier and any(t['id'] == identifier for t in tasks) else None
                tree = api('projects')['projects']
                try:
                    messages = api('conversation?project=' + quote(project) + '&id=' + conversation) if conversation else None
                except RuntimeError as error:
                    if 'Conversation does not belong' not in str(error):
                        raise
                    messages = {'deleted': conversation}
                return status, tasks, detail, tree, messages
            def refreshed(result):
                self.refreshing = False
                if epoch == self.history_epoch:
                    self.refreshed(result)
                else:
                    self.refresh()
            def failed(error):
                self.refreshing = False
                if epoch == self.history_epoch:
                    self.error.set_text(str(error))
                    self.error.set_visible(True)
            self.background(fetch, refreshed, failed)
            return True

        def refreshed(self, result):
            self.refreshing = False
            status, self.tasks, detail, tree, messages = result
            if detail:
                self.tasks = [detail if t['id'] == detail['id'] else t for t in self.tasks]
            self.render_tree(tree)
            self.render_quota(status.get('quota', {}))
            self.render_free_quota(status.get('free_quota', {}))
            if messages:
                if 'deleted' in messages:
                    if messages['deleted'] == self.conversation:
                        self.new_task()
                else:
                    self.render_messages(messages)
            if not status.get("checking"):
                if not self.connected:
                    self.connected = True
                    print("Native app connected: hardware status and shared task history loaded.", flush=True)
                gpu = status.get("gpu")
                self.hardware.set_text('NVIDIA driver active · Live hardware status' if gpu else 'CPU inference · Live hardware status')
            self.render_history()
            self.render_models(status)
            self.render_accounts(status)
            self.connection.set_text('●  Connected locally')
            self.run_button.set_sensitive(not any(t["status"] in dashboard.ACTIVE for t in self.tasks))
            if detail:
                if self.selected is None and self.conversation and detail.get('conversation') == self.conversation and detail["status"] in dashboard.ACTIVE:
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
                group = Gtk.Expander()
                project_label = Gtk.Label(label=project['name'], xalign=0, ellipsize=Pango.EllipsizeMode.END)
                project_label.set_width_chars(1)
                group.set_label_widget(project_label)
                group.set_tooltip_text(project['project'])
                group.set_expanded(project['project'] == self.conversation_project or expanded.get(project['project'], len(projects) == 1))
                chats = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                chats.set_margin_start(12)
                def new_in_project(_, path=project['project']):
                    self.new_task()
                    self.scope.set_selected(0 if hub.is_general(path) else 1)
                    self.project.set_text(path)
                    self.save_draft()
                chats.append(self.button('+ New chat', new_in_project, 'project-new-chat'))
                for chat in project['conversations']:
                    title = chat['goal'].replace('\n', ' ')
                    button = self.button(title,
                        lambda _, p=project['project'], c=chat['id']: self.open_chat(p, c))
                    button.get_child().set_ellipsize(Pango.EllipsizeMode.END)
                    button.get_child().set_width_chars(1)
                    button.get_child().set_xalign(0)
                    button.set_tooltip_text(title)
                    if chat['id'] == self.conversation:
                        button.add_css_class('active-chat')
                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
                    row.add_css_class('chat-row')
                    button.set_hexpand(True)
                    row.append(button)
                    remove = Gtk.Button.new_from_icon_name('user-trash-symbolic')
                    remove.add_css_class('chat-delete')
                    remove.set_opacity(0)
                    motion = Gtk.EventControllerMotion()
                    focus = Gtk.EventControllerFocus()
                    motion.connect('enter', lambda *_, target=remove: target.set_opacity(1))
                    motion.connect('leave', lambda *_, target=remove, focus=focus: target.set_opacity(1 if focus.contains_focus() else 0))
                    focus.connect('enter', lambda *_, target=remove: target.set_opacity(1))
                    focus.connect('leave', lambda *_, target=remove, motion=motion: target.set_opacity(1 if motion.contains_pointer() else 0))
                    row.add_controller(motion)
                    row.add_controller(focus)
                    remove.set_tooltip_text('Delete chat: ' + title)
                    remove.connect('clicked', lambda _, p=project['project'], c=chat['id'], title=title: self.confirm_delete_chat(p, c, title))
                    row.append(remove)
                    chats.append(row)
                group.set_child(chats)
                self.project_tree.append(group)

        def open_chat(self, project, identifier, task=None):
            epoch = self.history_epoch
            def opened(data):
                if epoch != self.history_epoch:
                    return
                self.follow_chat = True
                self.conversation = identifier
                self.conversation_project = project
                self.scope.set_selected(0 if data.get('scope') == 'general' else 1)
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

        def confirm_delete_chat(self, project, identifier, title):
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                message_type=Gtk.MessageType.QUESTION, text='Delete this chat?',
                secondary_text='“' + title[:160] + '”\n\nThis removes its local messages and task logs. Project files and shared memory are kept. This cannot be undone.')
            dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
            button = dialog.add_button('Delete chat', Gtk.ResponseType.ACCEPT)
            button.add_css_class('destructive-action')
            dialog.set_default_response(Gtk.ResponseType.CANCEL)
            def response(window, choice):
                window.destroy()
                if choice == Gtk.ResponseType.ACCEPT:
                    self.delete_chat(project, identifier)
            dialog.connect('response', response)
            self.delete_dialog = dialog
            dialog.present()

        def delete_chat(self, project, identifier):
            if self.deleting_chat:
                return
            self.deleting_chat = True
            self.history_epoch += 1
            def deleted(result):
                self.deleting_chat = False
                self.history_epoch += 1
                self.tasks = [t for t in self.tasks if t.get('conversation') != identifier]
                if self.conversation == identifier:
                    self.new_task()
                self.tree_signature = None
                self.history_signature = None
                self.refresh()
            def failed(error):
                self.deleting_chat = False
                self.error.set_text(str(error))
                self.error.set_visible(True)
                self.refresh()
            self.background(lambda: api('conversation/delete', {'project': project, 'id': identifier}), deleted, failed)

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
                self.update_pending_indicator(active)
                return
            previous_scroll = self.chat_scroll.get_vadjustment().get_value()
            previous_height = self.chat_scroll.get_vadjustment().get_upper()
            earlier = bool(all_turns and data['turns'] and data['turns'][-1]['rowid'] < all_turns[-1]['rowid'])
            self.message_signature = signature
            self.chat_title.set_text(data['goal'].split('\n')[0].split('. ')[0][:80])
            self.sync_chat_layout()
            self.chat_scroll.set_visible(True)
            self.clear_box(self.chat_messages)
            def add(role, text, status='', turn=None):
                user = role == 'You'
                row = self.row(0)
                row.add_css_class('message-row')
                row.set_hexpand(True)
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                card.set_halign(Gtk.Align.END if user else Gtk.Align.FILL)
                card.set_hexpand(not user)
                card.add_css_class('user-message' if user else 'assistant-message')
                speaker = self.label(role, 'chat-title')
                speaker.set_xalign(1 if user else 0)
                card.append(speaker)
                for kind, value in ([('paragraph', text)] if user else blocks(text, with_languages=True)):
                    language, value = value if kind == 'code' else ('', value)
                    content = self.label(value)
                    content.set_selectable(True)
                    content.set_max_width_chars(56 if user else -1)
                    content.add_css_class('reply-text')
                    if not user and kind != 'code':
                        content.set_markup('<span line_height="1.5">' + inline_markup(value) + '</span>')
                    if kind == 'heading':
                        content.add_css_class('reply-heading')
                    if kind == 'code':
                        content.add_css_class('reply-code')
                        content.set_wrap(False)
                        code_scroll = Gtk.ScrolledWindow(max_content_height=220, propagate_natural_height=True)
                        code_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                        code_scroll.set_child(content)
                        code_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                        code_box.add_css_class('reply-code-frame')
                        code_heading = self.row(12)
                        code_heading.add_css_class('reply-code-heading')
                        code_title = self.label(language or 'Code', 'footnote')
                        code_title.set_hexpand(True)
                        code_heading.append(code_title)
                        code_heading.append(self.message_action('Copy', 'edit-copy-symbolic', lambda *_, code=value: Gdk.Display.get_default().get_clipboard().set(code)))
                        code_box.append(code_heading)
                        code_box.append(code_scroll)
                        card.append(code_box)
                    else:
                        card.append(content)
                if not user and turn:
                    actions = self.row(12)
                    actions.append(self.message_action('Copy reply', 'edit-copy-symbolic', lambda *_: Gdk.Display.get_default().get_clipboard().set(text)))
                    if turn.get('has_review'):
                        actions.append(self.message_action('View changes', 'document-edit-symbolic', lambda *_, task=turn['task']: self.open_changes(task)))
                    card.append(actions)
                if status and status != 'completed':
                    card.append(self.label(status.replace('_', ' ').capitalize(), 'footnote'))
                gutter = Gtk.Box(hexpand=user, width_request=65 if user else 20)
                if user:
                    row.append(gutter)
                row.append(card)
                if not user:
                    row.append(gutter)
                self.chat_messages.append(row)
            if all_turns and data['total_turns'] > len(all_turns):
                before = all_turns[0]['rowid']
                endpoint = 'conversation?project=' + quote(self.conversation_project) + '&id=' + self.conversation + '&before=' + str(before)
                self.chat_messages.append(self.button('Load earlier messages', lambda *_: self.background(lambda: api(endpoint), self.render_messages)))
            for turn in all_turns:
                add('You', turn['request'])
                add('Coding Hub', turn['result'] or 'No final response was saved.', turn['status'], turn)
            if active:
                add('You', active['prompt'])
                indicator = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                indicator.add_css_class('pending-message')
                line = self.row(8)
                spinner = Gtk.Spinner(spinning=True)
                spinner.set_size_request(16,16)
                title = self.label('Working…', 'pending-title')
                line.append(spinner); line.append(title)
                detail = self.label('', 'muted')
                indicator.append(line); indicator.append(detail)
                self.chat_messages.append(indicator)
                self.pending_indicator = (active['id'], title, detail)
                self.update_pending_indicator(active)
            else:
                self.pending_indicator = None
            if self.follow_chat and not earlier:
                self.queue_chat_scroll()
            else:
                self.scroll_pending = True
                def preserve_position():
                    adjustment = self.chat_scroll.get_vadjustment()
                    adjustment.set_value(previous_scroll + (max(0, adjustment.get_upper() - previous_height) if earlier else 0))
                    self.scroll_pending = False
                    self.chat_scrolled(adjustment)
                    return False
                GLib.idle_add(preserve_position, priority=GLib.PRIORITY_LOW)

        def update_pending_indicator(self, task):
            if not task or not self.pending_indicator or self.pending_indicator[0] != task['id']:
                return
            progress = task.get('progress') or dashboard.task_progress(task, task.get('output', ''))
            self.pending_indicator[1].set_text(progress['label'])
            elapsed = max(0, int(time.time() - (task.get('started_at') or task['created_at'])))
            self.pending_indicator[2].set_text(str(elapsed // 60) + 'm ' + str(elapsed % 60) + 's · ' + progress['detail'])

        def message_action(self, text, icon, callback):
            button = self.button('', callback, 'message-action')
            row = self.row(5)
            picture = Gtk.Image.new_from_icon_name(icon)
            picture.set_pixel_size(12)
            row.append(picture)
            title = self.label(text)
            title.set_wrap(False)
            row.append(title)
            button.set_child(row)
            button.set_tooltip_text(text)
            return button

        def open_web(self, *_):
            self.open_web_button.set_sensitive(False)
            self.open_web_button.set_label('Opening…')
            def finished(ok, error):
                if self.closed_window: return
                self.open_web_button.set_sensitive(True)
                self.open_web_button.set_label('Open web ↗')
                if not ok:
                    self.error.set_text('Could not open your browser. Check the default web browser in Linux Settings → Default Apps, then try again.' + (' ' + str(error)[:180] if error else ''))
                    self.error.set_visible(True)
            try:
                (browser_launcher or launch_browser)(Gtk, Gdk, Gio, self.window, url, finished)
            except Exception as error:
                finished(False, str(error))

        def open_changes(self, task):
            project = self.conversation_project
            def loaded(review):
                window = Gtk.Window(title='Task changes', transient_for=self.window, modal=True, default_width=1040, default_height=720)
                window.add_css_class('coding-hub')
                if self.theme_switch.get_active(): window.add_css_class('dark')
                self.review_window = window
                body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
                body.set_margin_top(20); body.set_margin_bottom(20); body.set_margin_start(20); body.set_margin_end(20)
                heading = self.row(12)
                title = self.label('Task changes', 'page-title'); title.set_hexpand(True)
                heading.append(title); heading.append(self.button('Close', lambda *_: window.close())); body.append(heading)
                files = review['files']
                body.append(self.label(str(len(files)) + ' files · +' + str(sum(f['added'] for f in files)) + ' −' + str(sum(f['removed'] for f in files)) + (' · Partial review' if review.get('limited') else ''), 'muted'))
                body.append(self.label(review['note'] + (' Some files or diffs exceeded the review limits.' if review.get('limited') else ''), 'footnote'))
                split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
                listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                left = Gtk.ScrolledWindow(); left.set_size_request(200, -1); left.set_child(listing)
                split.set_start_child(left); split.set_position(230)
                right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                file_title = self.label('', 'section-title'); right.append(file_title)
                diff = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE)
                diff.add_css_class('review-code')
                buffer = diff.get_buffer()
                dark = self.theme_switch.get_active()
                for name, color, bg in [('added', '#afdcb2' if dark else '#205a2c', '#203e2a' if dark else '#dff1e1'), ('removed', '#f1b9b3' if dark else '#843330', '#482a2a' if dark else '#f9e3e2'), ('hunk', '#a9cbe3' if dark else '#38668f', '#20323f' if dark else '#e9f0f6')]:
                    buffer.create_tag(name, foreground=color, paragraph_background=bg)
                scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True); scroll.set_child(diff); right.append(scroll)
                right.append(self.button('Copy diff', lambda *_: Gdk.Display.get_default().get_clipboard().set(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))))
                split.set_end_child(right); body.append(split)
                buttons = []
                def select(file, index):
                    file_title.set_text(file['path']); buffer.set_text('')
                    for i, button in enumerate(buttons):
                        button.add_css_class('selected') if i == index else button.remove_css_class('selected')
                    for line in file['diff'].splitlines(keepends=True):
                        tag = 'hunk' if line.startswith('@@') else 'added' if line.startswith('+') and not line.startswith('+++') else 'removed' if line.startswith('-') and not line.startswith('---') else None
                        if tag: buffer.insert_with_tags_by_name(buffer.get_end_iter(), line, tag)
                        else: buffer.insert(buffer.get_end_iter(), line)
                for index, file in enumerate(files):
                    b = self.button('', lambda *_, f=file, i=index: select(f, i))
                    item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                    name = self.label(file['path']); name.set_max_width_chars(26); item.append(name)
                    item.append(self.label(file['status'] + ' · +' + str(file['added']) + ' −' + str(file['removed']), 'footnote'))
                    b.set_child(item); buttons.append(b); listing.append(b)
                if files: select(files[0], 0)
                else: buffer.set_text('No captured source changes.')
                window.set_child(body); window.present()
            self.background(lambda: api('changes?project=' + quote(project) + '&task=' + quote(task)), loaded)

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
            if name == 'workspace':
                # One scrollable transcript with a stationary composer.
                box.set_vexpand(True)
                self.stack.add_titled(box, name, title)
            else:
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
            self.hardware_bars = {}
            for key, title in (('gpu', 'Graphics'), ('gpu_load', 'GPU activity'), ('cpu', 'CPU activity'), ('vram', 'GPU memory'), ('ram', 'System memory'), ('placement', 'Local model placement')):
                row = self.row()
                left = self.label(title, 'muted')
                left.set_hexpand(True)
                row.append(left)
                value = self.label('Checking…', 'value')
                row.append(value)
                self.hardware_values[key] = value
                hardware.append(row)
                if key in ('gpu_load', 'cpu', 'vram', 'ram'):
                    bar = Gtk.ProgressBar()
                    hardware.append(bar)
                    self.hardware_bars[key] = bar
            self.hardware_age = self.label('Waiting for a live sample', 'footnote')
            hardware.append(self.hardware_age)
            box.append(hardware)
            doctor = self.card(12)
            self.doctor_button = self.button('Run system check', self.diagnose_system)
            doctor.append(self.section_heading('System diagnostics', self.doctor_button))
            doctor.append(self.label('Read-only checks for disk space, memory, GPU, services, and time settings.', 'muted'))
            self.doctor_report = self.label('', 'muted')
            self.doctor_report.set_selectable(True)
            self.doctor_report.set_visible(False)
            doctor.append(self.doctor_report)
            self.doctor_discuss = self.button('Discuss in general chat', self.discuss_diagnostics)
            self.doctor_discuss.set_visible(False)
            doctor.append(self.doctor_discuss)
            box.append(doctor)
            box.append(self.section_heading('Provider access', self.button('View usage & limits →', lambda *_: self.stack.set_visible_child_name('usage'))))
            box.append(self.label('Cloud routes use provider limits. Local Qwen has no provider quota; speed depends on your hardware. Releasing the local model frees memory and it loads again when needed.', 'muted'))

        def diagnose_system(self, *_):
            self.doctor_button.set_sensitive(False)
            self.doctor_report.set_visible(True)
            self.doctor_report.set_text('Checking your system…')
            def loaded(data):
                self.diagnostics = data['text']
                self.doctor_report.set_text(self.diagnostics)
                self.doctor_button.set_sensitive(True)
                self.doctor_discuss.set_visible(True)
            def failed(error):
                self.doctor_button.set_sensitive(True)
                self.doctor_report.set_text('Could not check the system: ' + str(error))
            self.background(lambda: api('diagnostics'), loaded, failed)

        def discuss_diagnostics(self, *_):
            import system_diagnostics
            self.new_task()
            self.edits.set_active(False)
            self.prompt.get_buffer().set_text(system_diagnostics.prompt(self.diagnostics))
            self.save_draft()

        def render_models(self, status):
            if status.get('checking'):
                return
            self.model_metrics['cloud'][0].set_text('Installed' if status.get('programs', {}).get('agy') else 'Setup needed')
            self.model_metrics['free'][0].set_text('Check unavailable' if status.get('pricing_error') else str(len(status.get('free_models', []))) + ' verified models')
            self.model_metrics['local'][0].set_text(('GPU ready' if status.get('gpu') else 'CPU ready') if status.get('local_ready') else 'Setup needed')
            gpu, ram = status.get('gpu'), status.get('ram')
            self.hardware_values['gpu'].set_text(gpu['name'].replace('NVIDIA GeForce ', '') if gpu else 'CPU inference')
            self.hardware_values['vram'].set_text(f"{gpu['used_mb'] / 1024:.1f} / {gpu['total_mb'] / 1024:.0f} GB" if gpu else 'Not available')
            self.hardware_values['ram'].set_text(f"{ram['total_gb']-ram['available_gb']:.1f} / {ram['total_gb']} GB used" if ram else 'Not available')
            gpu_load, cpu = (gpu or {}).get('utilization'), status.get('cpu')
            self.hardware_values['gpu_load'].set_text(f'{gpu_load}% active' if gpu_load is not None else 'Not available')
            self.hardware_values['cpu'].set_text(f'{cpu:.0f}% active' if cpu is not None else 'Waiting for sample')
            for key, value in {'gpu_load': (gpu_load or 0)/100, 'cpu': (cpu or 0)/100,
                               'vram': gpu['used_mb']/max(gpu['total_mb'],1) if gpu else 0,
                               'ram': 1-ram['available_gb']/max(ram['total_gb'],1) if ram else 0}.items():
                self.hardware_bars[key].set_fraction(max(0, min(1, value)))
            stamp = status.get('sampled_at')
            self.hardware_age.set_text('Live · sampled ' + datetime.fromtimestamp(stamp).strftime('%H:%M:%S') + ' · refreshes about every 3–6 seconds' if stamp else 'Waiting for live metrics')
            loaded = next((m for m in status.get('loaded', []) if m.get('name') == status.get('local_model') or m.get('model') == status.get('local_model')), next(iter(status.get('loaded', [])), None))
            self.loaded_model = (loaded or {}).get('name') or (loaded or {}).get('model') or hub.LOCAL_AGENT_MODEL
            placement = 'Sleeping · loads when needed'
            if loaded:
                placement = self.loaded_model + ' · ' + (f"{round(loaded.get('size_vram', 0) / max(loaded.get('size', 1), 1) * 100)}% GPU · remainder on CPU" if loaded.get('size_vram') else 'CPU only')
            self.hardware_values['placement'].set_text(placement)
            self.hardware.set_text(('GPU acceleration in use' if loaded and loaded.get('size_vram') else 'Local model running on CPU') if loaded else ('GPU available · local model is sleeping' if gpu else 'Local inference uses CPU'))
            self.release_button.set_sensitive(bool(loaded) and not any(t['status'] in dashboard.ACTIVE for t in self.tasks))

        def build_accounts_page(self):
            box = self.page_box('Accounts', 'accounts', 'Connect once. Continue your work here.')
            self.account_rows = {}
            for key, title, note in (
                ('antigravity', 'Antigravity · Google', 'Includes the Gemini, Claude and GPT models your Antigravity plan provides. Choose Google OAuth for a personal account.'),
                ('claude', 'Claude subscription', 'Optional separate Claude account. Uses the official Claude CLI and your existing subscription.'),
                ('openai', 'ChatGPT subscription', 'Optional separate ChatGPT account through OpenCode. Choose ChatGPT sign-in; API-key billing is not enabled here.')):
                card = self.card(8)
                button = self.button('Sign in', lambda _, provider=key: self.start_signin(provider), 'primary')
                card.append(self.section_heading(title, button))
                card.append(self.label(note, 'muted'))
                state = self.label('Checking installed CLI…', 'footnote')
                card.append(state)
                if key == 'antigravity':
                    reconnect = self.button('Reconnect Google account', lambda *_: self.start_signin('antigravity', True))
                    reconnect.set_tooltip_text('Signs out of the saved Antigravity session and opens Google sign-in again')
                    reconnect.set_halign(Gtk.Align.START)
                    card.append(reconnect)
                    card.append(self.label('Reconnect signs out of the saved Antigravity session first. Use it when authentication has expired.', 'footnote'))
                self.account_rows[key] = (state, button)
                box.append(card)
            self.signin_panel = self.card(10)
            self.signin_panel.set_visible(False)
            self.signin_title = self.label('Account sign-in', 'section-title')
            self.signin_panel.append(self.section_heading('Sign-in', self.button('Done / close', self.close_signin)))
            self.signin_panel.append(self.signin_title)
            self.signin_panel.append(self.label('Follow the provider prompts below. Complete Google, Claude or ChatGPT authentication in your browser, then return here. This panel is temporary and is not saved to chat history.', 'muted'))
            self.signin_screen = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE)
            self.signin_screen.add_css_class('signin-screen')
            scroll = Gtk.ScrolledWindow(min_content_height=280)
            scroll.set_child(self.signin_screen)
            self.signin_panel.append(scroll)
            controls = self.row(8)
            for key, label in (('up', '↑'), ('down', '↓'), ('enter', 'Enter'), ('tab', 'Tab'), ('escape', 'Esc')):
                button = self.button(label, lambda _, k=key: self.signin_send({'key': k}))
                button.set_tooltip_text('Send ' + key + ' to the sign-in prompt')
                controls.append(button)
            self.signin_panel.append(controls)
            self.signin_links = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self.signin_panel.append(self.signin_links)
            row = self.row(8)
            self.signin_input = Gtk.PasswordEntry(hexpand=True, show_peek_icon=True)
            self.signin_input.set_property('placeholder-text', 'Code or response requested by the provider')
            self.signin_input.connect('activate', self.signin_text)
            row.append(self.signin_input)
            row.append(self.button('Send response', self.signin_text))
            self.signin_panel.append(row)
            box.append(self.signin_panel)
            box.append(self.label('Account limits belong to each provider. Separate account routes are never selected by Automatic or Smart. Credentials stay in the provider’s own storage.', 'footnote'))

        def render_accounts(self, status):
            installed = {item['id']: item['installed'] for item in status.get('accounts', [])}
            states = status.get('account_status', {})
            for key, (label, button) in self.account_rows.items():
                ready = installed.get(key, False)
                state = states.get(key)
                if key == 'antigravity':
                    quota = status.get('quota', {})
                    state = 'connected' if quota.get('available') and not quota.get('error') and not quota.get('stale') else 'unknown'
                text = {'connected': 'Connected', 'saved': 'Sign-in saved · provider confirms access when used',
                        'sign_in': 'Sign in to connect', 'unknown': 'Installed · sign in or check access'}.get(state, 'Installed · checking access')
                label.set_text(text if ready else 'CLI not installed on this computer')
                button.set_sensitive(ready and not self.signin_id)
                button.set_label('Manage sign-in' if state in ('connected', 'saved') else 'Sign in')
            self.model_choices = dict(status.get('provider_models', {}), free=[{'id': mid, 'name': mid} for mid in status.get('free_models', [])], local=[{'id': mid, 'name': mid} for mid in status.get('models', [])])
            self.render_model_picker()

        def start_signin(self, provider, reconnect=False):
            self.error.set_visible(False)
            def started(data):
                self.signin_id = data['id']
                self.signin_panel.set_visible(True)
                self.signin_title.set_text({'antigravity': 'Antigravity · Google OAuth', 'claude': 'Claude subscription', 'openai': 'ChatGPT subscription'}[provider])
                self.signin_link_signature = None
                self.show_signin(data)
                # Scroll the accounts page to its active sign-in panel.
                self.signin_input.grab_focus()
            self.background(lambda: api('accounts/start', {'provider': provider, 'reconnect': reconnect}), started)

        def show_signin(self, data):
            if data['id'] != self.signin_id:
                return
            self.signin_screen.get_buffer().set_text(data.get('screen') or 'Opening the provider’s sign-in flow…')
            links = data.get('links', [])
            if links != getattr(self, 'signin_link_signature', None):
                self.signin_link_signature = links
                self.clear_box(self.signin_links)
                for link in links:
                    button = Gtk.LinkButton.new_with_label(link, 'Open provider sign-in in browser ↗')
                    button.set_halign(Gtk.Align.START)
                    self.signin_links.append(button)
            if not data.get('running'):
                self.signin_title.set_text('Sign-in command finished · choose Done to refresh account status' if data.get('exit_code') == 0 else 'Sign-in ended · close and try again if access is not connected')

        def poll_signin(self):
            if self.closed_window:
                return False
            if not self.signin_id or self.signin_busy:
                return True
            self.signin_busy = True
            identifier = self.signin_id
            def fetch():
                try:
                    return api('accounts/session?id=' + identifier)
                except Exception:
                    return None
            def result(data):
                self.signin_busy = False
                if data:
                    self.show_signin(data)
            self.background(fetch, result)
            return True

        def signin_send(self, data):
            if self.signin_id:
                self.background(lambda: api('accounts/input', dict(data, id=self.signin_id)), lambda _: None)

        def signin_text(self, *_):
            text = self.signin_input.get_text()
            self.signin_input.set_text('')
            if text:
                self.signin_send({'text': text})

        def close_signin(self, *_):
            identifier = self.signin_id
            if not identifier:
                return
            def closed(_):
                self.signin_id = None
                self.signin_panel.set_visible(False)
                self.signin_screen.get_buffer().set_text('')
                self.signin_input.set_text('')
                self.clear_box(self.signin_links)
                self.refresh()
            self.background(lambda: api('accounts/close', {'id': identifier}), closed)

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
            if hasattr(self, 'sidebar_tools'):
                self.sidebar_tools.set_expanded(name != 'workspace')
            titles = {'workspace': 'Chats', 'history': 'Task history', 'models': 'Models & hardware', 'usage': 'Usage & limits', 'memory': 'Project memory', 'accounts': 'Accounts'}
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
            if self.sidebar_save_source:
                GLib.source_remove(self.sidebar_save_source)
                self.sidebar_save_source = None
            self.save_preferences(sidebar_width=self.workspace_split.get_position())
            self.closed_window = True
            self.executor.shutdown(wait=False, cancel_futures=True)
            hub.save_json(hub.STATE / "desktop" / "window.json",
                          {"pid": os.getpid(), "status": "closed", "closed_at": time.time()})
            return False

    return CodingHub()


if __name__ == "__main__":
    sys.exit(launch())

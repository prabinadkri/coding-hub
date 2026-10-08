"""A native Linux application window sharing the local Coding Hub dashboard."""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
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
    routes = ("auto", "antigravity", "free", "local")

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
            self.window.set_default_size(1180, 900)
            self.window.set_icon_name("coding-hub")
            self.window.connect("close-request", self.closed)
            css = Gtk.CssProvider()
            css.load_from_data(b"""
                window { background: #f5f6f0; color: #19352b; }
                headerbar { background: #173f32; color: #ffffff; }
                headerbar button { color: #ffffff; background: #285642; }
                .heading { font-size: 27px; font-weight: 700; }
                .muted { color: #61746a; }
                .section { font-size: 11px; font-weight: 700; letter-spacing: 1px; }
                .status { padding: 12px; background: #e6eee2; border-radius: 10px; }
                .error { color: #a12d29; }
                .primary { background: #22553f; color: white; font-weight: 700; }
                .output text, .output { background: #152b23; color: #e4eee5; }
                textview { padding: 12px; border-radius: 8px; }
                button { padding: 9px 14px; border-radius: 8px; }
                entry { padding: 8px; border-radius: 8px; }
            """)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            header = Gtk.HeaderBar()
            header.set_title_widget(Gtk.Label(label="Coding Hub"))
            header.pack_start(self.button("New task", self.new_task))
            header.pack_end(self.button("Open web", lambda *_: webbrowser.open(url)))
            self.window.set_titlebar(header)
            keyboard = Gtk.EventControllerKey()
            keyboard.connect("key-pressed", self.shortcut)
            self.window.add_controller(keyboard)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
            for edge in ("top", "bottom", "start", "end"):
                getattr(body, "set_margin_" + edge)(22)
            self.window.set_child(body)
            title = self.row()
            picture = Gtk.Image.new_from_file(str(hub.ROOT / "assets" / "icon.svg"))
            picture.set_pixel_size(48)
            title.append(picture)
            titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            titles.append(self.label("Your coding workspace", "heading"))
            titles.append(self.label("Cloud when you need it. Local when you want it.", "muted"))
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
            project_row.append(self.button("Browse…", self.browse))
            body.append(project_row)
            body.append(self.label("YOUR TASK", "section"))
            self.prompt = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            prompt_scroll = Gtk.ScrolledWindow(min_content_height=125)
            prompt_scroll.set_child(self.prompt)
            body.append(prompt_scroll)
            options = self.row()
            self.route = Gtk.DropDown.new_from_strings(["Automatic", "Antigravity", "Free cloud", "Local Qwen"])
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
            self.run_button = self.button("Run task", self.run_task, "primary")
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
            body.append(output_scroll)
            body.append(self.label("Closing this window keeps tasks running. App and web share task history.", "muted"))
            try:
                draft = json.loads(self.draft_path.read_text())
                self.project.set_text(draft.get("project", str(Path.home() / "Documents")))
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
                    "mode": "build" if self.edits.get_active() else "analysis"}

        def save_draft(self):
            if self.closed_window:
                return False
            hub.save_json(self.draft_path, self.form())
            return True

        def route_changed(self, *_):
            selected = self.route.get_selected()
            if hasattr(self, "quality"):
                self.quality.set_sensitive(selected < 2)
            if hasattr(self, "hint"):
                self.hint.set_text([
                    "Antigravity → verified free models → local Qwen. Continues from partial work if a provider fails.",
                    "Uses your Google sign-in. Fast selects Flash; Deep selects Pro. Provider quotas apply.",
                    "Checks current zero-cost pricing before each run. Provider quotas apply.",
                    "Qwen3 8B runs on this computer with 16K context. Best for focused tasks."
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
            def fetch():
                status = api("status")
                tasks = api("tasks")["tasks"]
                identifier = selected or next((t["id"] for t in tasks if t["status"] in dashboard.ACTIVE), None)
                detail = api("tasks/" + identifier) if identifier else None
                return status, tasks, detail
            self.background(fetch, self.refreshed)
            return True

        def refreshed(self, result):
            self.refreshing = False
            status, self.tasks, detail = result
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

        def closed(self, window):
            self.save_draft()
            self.closed_window = True
            self.executor.shutdown(wait=False, cancel_futures=True)
            hub.save_json(hub.STATE / "desktop" / "window.json",
                          {"pid": os.getpid(), "status": "closed", "closed_at": time.time()})
            return False

    return CodingHub().run([])


if __name__ == "__main__":
    sys.exit(launch())

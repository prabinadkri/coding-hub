"""Exercise native sidebar startup independently of provider and chat loading."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop
import hub


def main():
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, Gdk, Gio, GLib

    def wait_for(check, message):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            if check():
                return
            time.sleep(.01)
        raise AssertionError(message)

    projects = [{'project': '/workspace/' + name, 'name': name, 'conversations': [
        {'id': name + '-chat', 'goal': 'Saved ' + name + ' chat'}]} for name in ('first', 'second')]
    release = threading.Event()
    calls = []
    fail_projects = False

    def api(path, body=None):
        calls.append((path, body))
        if path == 'projects':
            if fail_projects:
                raise RuntimeError('Temporary sidebar connection failure')
            return {'projects': projects}
        if path == 'status':
            release.wait(10)
            raise RuntimeError('Provider status unavailable during startup')
        raise AssertionError('Unexpected request: ' + path)

    with tempfile.TemporaryDirectory(prefix='coding-hub-startup-') as temporary:
        previous = hub.STATE
        hub.STATE = Path(temporary)
        app = desktop.create_application(Gtk, Gdk, Gio, GLib, api, 'http://127.0.0.1:8765/')
        try:
            app.register(None)
            app.activate()
            wait_for(lambda: isinstance(app.project_tree.get_first_child(), Gtk.Expander),
                     'Saved projects must load before the blocked status request finishes')
            first = app.project_tree.get_first_child()
            second = first.get_next_sibling()
            assert not first.get_expanded() and not second.get_expanded(), 'Do not auto-expand groups'
            assert first.get_child().get_first_child().get_next_sibling().get_first_child().get_label() == 'Saved first chat'
            assert second.get_child().get_first_child().get_next_sibling().get_first_child().get_label() == 'Saved second chat'
            assert app.refreshing, 'The status request should still be blocked'
            assert not any(body for _, body in calls), 'Showing history must not send a task'
            first.set_expanded(True)
            fail_projects = True
            app.refresh_sidebar()
            wait_for(lambda: not app.sidebar_refreshing, 'Sidebar failure should release its retry guard')
            assert app.project_tree.get_first_child() is first, 'Keep saved entries on a transient failure'
            assert first.get_expanded(), 'Retry must not change group expansion'
            assert app.sidebar_status.get_visible()
            fail_projects = False
            app.refresh_sidebar()
            wait_for(lambda: not app.sidebar_refreshing and not app.sidebar_status.get_visible(), 'Retry must recover without sending a message')
            release.set()
            wait_for(lambda: not app.refreshing, 'Failed provider status must finish independently')
            assert app.project_tree.get_first_child() is first
            print(json.dumps({'startup_sidebar': 'passed', 'provider_requests': 0, 'group_auto_expansion': False}))
        finally:
            release.set()
            app.closed(app.window)
            app.window.destroy()
            app.quit()
            hub.STATE = previous


if __name__ == '__main__':
    main()

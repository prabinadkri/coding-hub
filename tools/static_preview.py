"""Loopback static preview without directory listings, dotfiles or escaping symlinks."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

root = Path(sys.argv[1]).resolve()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(root), **kwargs)

    def send_head(self):
        parts = Path(unquote(urlsplit(self.path).path)).parts
        target = Path(self.translate_path(self.path)).resolve()
        if urlsplit(self.path).path == '/favicon.ico' and not target.is_file():
            self.send_response(204); self.end_headers()
            return None
        if any(part.startswith('.') for part in parts) or not target.is_relative_to(root):
            self.send_error(403)
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404)
        return None


ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2])), Handler).serve_forever()

"""Developer tool: run the app UI in a normal browser instead of the native window.

    python tools/browser_preview.py      then open http://127.0.0.1:8765

It serves dusterscan/ui and forwards window.pywebview.api calls to the real
Api class over HTTP. Native file dialogs are not available in this mode.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dusterscan.app import Api, _resource_dir  # noqa: E402

UI = _resource_dir()
events: Queue[str] = Queue()

SHIM = """
<script>
window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) => {
  const r = await fetch('/api/' + name, { method: 'POST', body: JSON.stringify(args) });
  return r.json();
} }) };
(async function poll() {
  while (true) {
    try { const r = await fetch('/events'); for (const js of await r.json()) eval(js); } catch (e) {}
    await new Promise(r => setTimeout(r, 250));
  }
})();
window.addEventListener('load', () => window.dispatchEvent(new Event('pywebviewready')));
</script>
"""


class FakeWindow:
    def evaluate_js(self, js: str) -> None:
        events.put(js)

    def create_file_dialog(self, *a, **k):
        return None


api = Api()
api._window = FakeWindow()  # type: ignore[assignment]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/events":
            out = []
            try:
                while True:
                    out.append(events.get_nowait())
            except Empty:
                pass
            return self._send(json.dumps(out).encode(), "application/json")
        name = self.path.strip("/") or "index.html"
        f = (UI / name).resolve()
        if UI not in f.parents or not f.is_file():
            self.send_error(404)
            return
        data = f.read_bytes()
        if name == "index.html":
            data = data.replace(b'<script src="app.js">', SHIM.encode() + b'<script src="app.js">')
        ctype = {"html": "text/html", "css": "text/css", "js": "text/javascript"}.get(f.suffix[1:], "text/plain")
        self._send(data, ctype + "; charset=utf-8")

    def do_POST(self):
        name = self.path.removeprefix("/api/")
        if name.startswith("_") or not hasattr(api, name):
            self.send_error(404)
            return
        args = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"[]")
        result = getattr(api, name)(*args)
        self._send(json.dumps(result).encode(), "application/json")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Open http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

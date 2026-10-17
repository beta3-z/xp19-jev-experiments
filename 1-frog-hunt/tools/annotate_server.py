"""Serve the annotation page and store hand-marked frog boxes."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "runs" / "report"
SHOTS = ROOT / "screenshots"
STORE = REPORT / "annotations.json"
WORLD = ROOT / "runs" / "world_scroll" / "world.jpg"
WORLD_STORE = REPORT / "world_annotations.json"
PORT = 8765


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send(b"not found", "text/plain", 404)
            return
        self._send(path.read_bytes(), content_type)

    def do_GET(self) -> None:
        route = unquote(self.path.split("?")[0])
        if route in ("/", "/index.html", "/annotate.html"):
            self._file(REPORT / "annotate.html", "text/html; charset=utf-8")
        elif route == "/world":
            self._file(REPORT / "annotate_world.html", "text/html; charset=utf-8")
        elif route == "/panorama":
            self._file(WORLD, "image/jpeg")
        elif route == "/world.json":
            self._file(REPORT / "world.json", "application/json")
        elif route == "/world_annotations":
            if WORLD_STORE.is_file():
                self._file(WORLD_STORE, "application/json")
            else:
                self._send(b"[]", "application/json")
        elif route == "/data.json":
            self._file(REPORT / "data.json", "application/json")
        elif route == "/annotations":
            if STORE.is_file():
                self._file(STORE, "application/json")
            else:
                self._send(b"{}", "application/json")
        elif route.startswith("/shots/"):
            self._file(SHOTS / route[len("/shots/"):], "image/jpeg")
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:
        route = self.path.split("?")[0]
        if route not in ("/annotations", "/world_annotations"):
            self._send(b"not found", "text/plain", 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        target = WORLD_STORE if route == "/world_annotations" else STORE
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"saved {len(payload)} entries -> {target}", flush=True)
        self._send(b'{"ok":true}', "application/json")

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"annotate http://127.0.0.1:{PORT}/", flush=True)
    server.serve_forever()

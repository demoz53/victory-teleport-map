"""modmap.py: a click-to-teleport map for the VIctory mod commands.

Serves one web page on this PC only (http://127.0.0.1:<port>/). The page shows the GTA VI map from
jeanropke's community map (https://jeanropke.github.io/GTAVIMap/; your browser loads its tiles from
their CDN), the GAME coordinates under the mouse, your player's live position (read from the mod
status file), and click-to-teleport. A teleport is one line `teleport x y [z]` appended to the mod
file; the game side (a VIctory build run with VICTORY_MOD=1) reads that file every frame and does
the rest.

    python modmap.py --mod-file <mod.txt> --status-file <status.txt> [--port 8766] [--open]
    python modmap.py --demo [--open]        # try the page without the game

Safety: it listens on 127.0.0.1 only, accepts only teleport commands made of numbers, and only
with the random key written into the page it serves, so another web page cannot send commands.
Python 3.8 or later, standard library only.
"""
import argparse
import http.server
import json
import math
import os
import secrets
import socketserver
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = secrets.token_hex(16)
STALE_S = 5.0  # the game rewrites the status file twice a second


def read_status(path):
    """The status file as the page wants it: {live, x, y, z, heading_deg, in_vehicle}."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            pairs = (l.split("=", 1) for l in f.read().splitlines() if "=" in l)
            st = {k.strip(): v.strip() for k, v in pairs}
    except OSError:
        return {"live": False}
    try:
        age = time.time() - int(st.get("updated_unix_ms", "0")) / 1000.0
    except ValueError:
        age = 1e9
    out = {"live": age < STALE_S and st.get("game") == "running"}
    for k in ("x", "y", "z", "heading_deg"):
        try:
            out[k] = float(st[k])
        except (KeyError, ValueError):
            pass
    out["in_vehicle"] = st.get("in_vehicle") == "1"
    return out


class Demo:
    """A pretend game for --demo: a player on the deck of Jason's house who turns slowly and
    jumps wherever you teleport. Nothing is written to disk."""

    def __init__(self):
        self.lock = threading.Lock()
        self.x, self.y, self.z, self.t0 = -2355.6, -5526.3, 7.2, time.time()

    def status(self):
        with self.lock:
            heading = (120 + 6 * (time.time() - self.t0)) % 360
            return {"live": True, "demo": True, "x": self.x, "y": self.y, "z": self.z,
                    "heading_deg": round(heading), "in_vehicle": False}

    def teleport(self, vals):
        with self.lock:
            self.x, self.y = vals[0], vals[1]
            self.z = vals[2] if len(vals) > 2 else 0.0


def make_handler(mod_file, status_file, demo):
    page = open(os.path.join(HERE, "map.html"), encoding="utf-8").read().replace("__KEY__", KEY)
    status = demo.status if demo else (lambda: read_status(status_file))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, "{}")
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, page, "text/html")
            if path == "/status":
                return self._send(200, json.dumps(status()))
            return self._send(404, "{}")

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Key") != KEY:
                return self._send(403, '{"ok":false,"error":"refused"}')
            origin = self.headers.get("Origin")
            if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                return self._send(403, '{"ok":false,"error":"refused"}')
            if self.path != "/teleport":
                return self._send(404, "{}")
            try:
                n = min(int(self.headers.get("Content-Length", "0")), 4096)
                req = json.loads(self.rfile.read(n) or b"{}")
                x, y = float(req["x"]), float(req["y"])
                z = req.get("z")
                z = None if z in (None, "") else float(z)
                vals = [x, y] + ([z] if z is not None else [])
                if not all(math.isfinite(v) and abs(v) < 100000 for v in vals):
                    raise ValueError("out of range")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}))
            line = "teleport " + " ".join(f"{v:.2f}" for v in vals)
            if demo:
                demo.teleport(vals)
                print(f"demo: {line}", flush=True)
                return self._send(200, json.dumps({"ok": True, "line": line + " (demo)"}))
            if not status().get("live"):
                return self._send(200, json.dumps({"ok": False, "error": "the game is not running (or VICTORY_MOD is off)", "line": line}))
            os.makedirs(os.path.dirname(os.path.abspath(mod_file)), exist_ok=True)
            with open(mod_file, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
            return self._send(200, json.dumps({"ok": True, "line": line}))

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second server bind the same port silently; fail instead.
    allow_reuse_address = False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mod-file", help="the file the game reads commands from (VICTORY_MOD_FILE)")
    ap.add_argument("--status-file", help="the file the game writes its state to (VICTORY_MOD_STATUS)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    ap.add_argument("--demo", action="store_true", help="no game: a pretend player that jumps where you teleport")
    a = ap.parse_args()
    if not a.demo and not (a.mod_file and a.status_file):
        ap.error("--mod-file and --status-file are needed (or --demo)")
    srv = Server(("127.0.0.1", a.port), make_handler(a.mod_file, a.status_file, Demo() if a.demo else None))
    url = f"http://127.0.0.1:{a.port}/"
    print(f"teleport map at {url}" + (" (demo, no game)" if a.demo else f" (mod file {a.mod_file})"), flush=True)
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

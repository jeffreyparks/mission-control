#!/usr/bin/env python3
"""
Local writeback server for the Mission Control dashboard.

    uv run serve.py            # http://127.0.0.1:8787

Serves artifacts/html/ and exposes a tiny JSON API so the dashboard can edit
fields that a human owns - status first. Writes go through agents/store.py, so
every change is logged and the spreadsheet is re-exported immediately.

The dashboard degrades on purpose: opened as a file:// page, or with the server
off, it is simply read-only. Nothing breaks, the dropdowns just do not appear.

Deliberate limits:
  - binds to loopback only, and refuses any client that is not loopback
  - only the fields in EDITABLE can be written
  - no shutdown, no shell, no arbitrary paths: static files are served from
    artifacts/html/ and nowhere else
"""
import argparse
import json
import sys
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "agents"))

from store import Store  # noqa: E402
from job_intel import load_archetypes, OUTCOME_LABELS  # noqa: E402

WEB_ROOT = BASE / "artifacts/html"

# Fields the dashboard may write, and the values it may write for them. A closed
# vocabulary keeps a stray request from inventing a status or a category that
# does not exist. status/priority/recommendation are fixed; role_cat and
# outcomes are computed at startup from your own data (career-goals.md and the
# tracker's existing labels), so they can never drift from what the rest of
# the pipeline understands.
EDITABLE = {
    "status": ["00 New find", "01 Open", "02 Researching", "03 Applied", "04 Closed"],
    "priority": ["1", "2", "3", ""],
    "recommendation": ["apply", "research", "skip", ""],
}


def _outcome_options():
    seen, options = set(), [""]
    for _needle, pretty in OUTCOME_LABELS:
        if pretty not in seen:
            seen.add(pretty)
            options.append(pretty)
    return options

_write_lock = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    store = None
    editable = EDITABLE
    quiet = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    # ---------- helpers ----------

    def _loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write(f"[{datetime.now():%H:%M:%S}] {fmt % args}\n")

    # ---------- routes ----------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            if not self._loopback():
                return self._json({"error": "loopback only"}, 403)
            return self._json({
                "ok": True,
                "rows": self.store.count(),
                "editable": self.editable,
                "db": str(self.store.db_path.relative_to(BASE)),
            })
        if path == "/api/changes":
            if not self._loopback():
                return self._json({"error": "loopback only"}, 403)
            return self._json({"changes": self.store.history(limit=50)})
        if path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._loopback():
            return self._json({"error": "loopback only"}, 403)
        if not path.startswith("/api/role/"):
            return self._json({"error": "not found"}, 404)

        role_id = path[len("/api/role/"):].strip("/")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad json"}, 400)

        field = payload.get("field")
        value = payload.get("value")
        if field not in self.editable:
            return self._json({"error": f"field not editable: {field}"}, 400)
        allowed = self.editable[field]
        # Validate against the vocabulary as text (a select element only ever
        # sends strings), then store the type the rest of the pipeline expects.
        if allowed is not None and str(value or "") not in allowed:
            return self._json({"error": f"value not allowed for {field}: {value!r}"}, 400)
        if field == "priority" and value not in (None, ""):
            value = float(value)

        try:
            with _write_lock:
                result = self.store.set_field(role_id, field, value or None, actor="dashboard")
                exported = self.store.export_xlsx() if result["changed"] else None
        except KeyError:
            return self._json({"error": f"unknown role: {role_id}"}, 404)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)

        return self._json({
            "ok": True,
            "id": role_id,
            "field": field,
            "changed": result["changed"],
            "old": result["old"],
            "new": result["new"],
            "exported": bool(exported),
        })


def serve(host="127.0.0.1", port=8787, base=BASE, quiet=False):
    store = Store(base)
    editable = dict(EDITABLE)
    editable["outcomes"] = _outcome_options()
    try:
        archetypes = load_archetypes(base, store.to_df())
        editable["role_cat"] = [""] + [a["label"] for a in archetypes]
    except Exception:  # noqa: BLE001 - a missing career-goals.md must not break serving
        editable["role_cat"] = [""]

    Handler.store = store
    Handler.editable = editable
    Handler.quiet = quiet
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if not WEB_ROOT.exists():
        print("no artifacts/html yet - run: uv run run_daily.py --only render")
        return 1

    httpd = serve(args.host, args.port)
    store = Handler.store
    print(f"Mission Control writeback server")
    print(f"  dashboard : http://{args.host}:{args.port}/job-tracker.html")
    print(f"  database  : {store.db_path.relative_to(BASE)} ({store.count()} roles)")
    print(f"  editable  : {', '.join(Handler.editable)}")
    print("  ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

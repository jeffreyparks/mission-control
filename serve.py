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
from job_intel import JobIntel, load_archetypes, OUTCOME_LABELS, _clean, _date, parse_outcome  # noqa: E402

sys.path.insert(0, str(BASE))
from render.build import _env, render_tracker as _render_tracker_page  # noqa: E402

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


def _overlay_live_values(intel_data, store):
    """Copy of the latest intel json with user-editable fields overlaid from the
    live database, keyed by store_id. Judgement fields (fit_score, why, sector,
    suggested category, ...) are not in the database and are left as the
    pipeline last computed them; only the fields the dashboard can write are
    refreshed, plus the org directory and recommendation counts that are
    derived from them.
    """
    df = store.to_df()
    by_id = {row["_id"]: row for row in df.to_dict("records") if row.get("_id")}

    roles = []
    for role in intel_data.get("roles", []):
        role = dict(role)
        row = by_id.get(role.get("store_id"))
        if row is not None:
            role["status"] = _clean(row.get("Status"))
            priority = _clean(row.get("Priority"))
            role["priority"] = int(float(priority)) if priority else None
            role["recommendation"] = _clean(row.get("Recommendation"))
            role["role_cat"] = _clean(row.get("Role Cat"))
            role["outcome"] = _clean(row.get("Outcomes"))
            label, days, display = parse_outcome(
                role["outcome"], row.get("Date Applied"), row.get("Last Updated"))
            role["outcome_label"], role["days_to_outcome"], role["outcome_display"] = label, days, display
            sector = _clean(row.get("Sector"))
            if sector:
                role["sector"] = sector
        roles.append(role)

    sectors = {}
    for role in roles:
        if role.get("org") and role.get("sector") and role["org"] not in sectors:
            sectors[role["org"]] = role["sector"]
    orgs = JobIntel.build_org_directory(roles, sectors)

    return {**intel_data, "roles": roles, "orgs": orgs}


def _latest_intel(base):
    """The newest intel-*.json for THIS base dir.

    Deliberately not render.build._load: that helper is anchored to build.py's
    own file location, which is always the real repo - fine for the daily
    render, wrong for tests that point a Store at a temp directory.
    """
    files = sorted(Path(base).glob("artifacts/jobs/intel-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except (json.JSONDecodeError, OSError):
        return None


def render_tracker_live(base):
    """The tracker page, rendered fresh from the database on every request.

    Falls back to whatever is on disk (or None) if there is no intel json yet,
    or if anything about the overlay fails - a rendering bug must never take
    the dashboard down.
    """
    intel_data = _latest_intel(base)
    if not intel_data:
        return None
    try:
        store = Store(base)
        overlaid = _overlay_live_values(intel_data, store)
        _path, html = _render_tracker_page(_env(), overlaid, write=False)
        return html
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"live tracker render failed, serving the static file: {exc}\n")
        return None


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
        # Per-instance, not the module-level WEB_ROOT: two servers can run in
        # the same process (tests do this) pointed at different base dirs, and
        # each must serve its own artifacts/html, not whichever ran last.
        root = str(self.store.base_dir / "artifacts/html") if self.store else str(WEB_ROOT)
        super().__init__(*args, directory=root, **kwargs)

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
        if path == "/job-tracker.html":
            html = render_tracker_live(self.store.base_dir)
            if html is not None:
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            # Live render unavailable (no intel json yet, or it failed) - fall
            # through to whatever static copy exists on disk, same as before
            # this feature existed.
        if path == "/api/health":
            if not self._loopback():
                return self._json({"error": "loopback only"}, 403)
            return self._json({
                "ok": True,
                "rows": self.store.count(),
                "editable": self.editable,
                # relative to this server's own base, not the module-level BASE
                # constant - those differ for a temp-dir store such as a test.
                "db": str(self.store.db_path.relative_to(self.store.base_dir)),
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

    # A fresh Handler subclass per server, not the shared base class: Handler.store
    # was a class attribute, so a second serve() call in the same process (as the
    # tests do) silently repointed the FIRST server at the SECOND server's database.
    # Binding state on a dedicated subclass keeps multiple servers independent.
    class _BoundHandler(Handler):
        pass
    _BoundHandler.store = store
    _BoundHandler.editable = editable
    _BoundHandler.quiet = quiet

    httpd = ThreadingHTTPServer((host, port), _BoundHandler)
    httpd.mc_store = store          # for callers (main(), tests) that want it back
    httpd.mc_editable = editable
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
    store = httpd.mc_store
    print(f"Mission Control writeback server")
    print(f"  dashboard : http://{args.host}:{args.port}/job-tracker.html")
    print(f"  database  : {store.db_path.relative_to(BASE)} ({store.count()} roles)")
    print(f"  editable  : {', '.join(httpd.mc_editable)}")
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

#!/usr/bin/env python3
"""
Local writeback server for the Mission Control dashboard.

    uv run dashboard.py                  # http://127.0.0.1:8787
    uv run dashboard.py --user ariel     # someone else's data, yours untouched

Serves the chosen workspace's artifacts/html/ and exposes a tiny JSON API so
the dashboard can edit fields that a human owns - status first. Pick the
workspace with --user NAME; it defaults to workspace/default.

Writes go through agents/store.py, so every change is logged straight into the
database - the only record.

The dashboard degrades on purpose: opened as a file:// page, or with the server
off, it is simply read-only. Nothing breaks, the dropdowns just do not appear.

Deliberate limits:
  - binds to loopback only, and refuses any client that is not loopback
  - only the fields in EDITABLE can be written, plus the profile settings
    agents/profile_settings.py validates (never a secret such as a password)
  - no shutdown, no shell, no arbitrary paths: static files are served from
    that workspace's artifacts/html/ and nowhere else
"""
import argparse
import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "agents"))


def _server_commit():
    """The commit this running process was started from, so a stale process -
    still serving code from before a `git pull` or an agent edit - is obvious
    from /api/health instead of silently behaving like an old version. Python
    does not hot-reload; every code change here needs `dashboard.py` restarted."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(BASE),
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


SERVER_COMMIT = _server_commit()
SERVER_STARTED_AT = datetime.now().isoformat(timespec="seconds")

from store import (CLOSE_REASONS, CONTACT_KINDS, STAGES, TOUCH_CHANNELS,  # noqa: E402
                   TOUCH_DIRECTIONS, Store)
from job_intel import JobIntel, load_archetypes, OUTCOME_LABELS, _clean, _date, parse_outcome  # noqa: E402

sys.path.insert(0, str(BASE))
from render.build import _env, render_profile as _render_profile_page  # noqa: E402
from render.build import render_tracker as _render_tracker_page  # noqa: E402
from render.build import render_settings as _render_settings_page  # noqa: E402
from render.build import render_applications as _render_applications_page  # noqa: E402
import profile_settings  # noqa: E402
import workspace  # noqa: E402

# Fields the dashboard may write, and the values it may write for them. A closed
# vocabulary keeps a stray request from inventing a status or a category that
# does not exist. status/priority/recommendation are fixed; role_cat and
# outcomes are computed at startup from your own data (me/profile.md and the
# tracker's existing labels), so they can never drift from what the rest of
# the pipeline understands.
EDITABLE = {
    "status": ["00 New find", "01 Open", "02 Researching", "03 Applied", "04 Closed"],
    "priority": ["1", "2", "3", ""],
    # Why you passed on a role, as opposed to Outcomes (what the employer did).
    "close_reason": [""] + CLOSE_REASONS,
    # recommendation is deliberately NOT editable: it is the model's fit
    # judgement, not a status you set - shown as a plain badge, same as Fit
    # Score. Status/Priority/Notes/Salary are yours to set; Recommendation
    # is the pipeline's read on the role, for you to weigh, not overwrite.
    # None means free text: any string is accepted (subject to FREE_TEXT_MAX_LEN
    # below), not a closed vocabulary. The dashboard renders these as a text
    # input rather than a dropdown.
    "notes": None,
    "comp_range": None,   # rendered as "Salary" in the dashboard
    # The Applications tab. Stage is where an applied role has got to; a
    # Next Action you type overrides the suggested one, and a Next Action Due
    # on its own snoozes it.
    "stage": [""] + STAGES,
    "next_action": None,
    "next_action_due": None,
    "date_applied": None,
}

FREE_TEXT_MAX_LEN = {"notes": 2000, "comp_range": 120, "next_action": 200}

# Fields where saving the value unchanged still means something: you checked
# it. Date Applied starts life as an estimate; keeping it confirms it.
CONFIRMABLE = {"date_applied"}

# Free-text fields that must hold a calendar date (YYYY-MM-DD) or be empty.
DATE_FIELDS = {"date_applied", "next_action_due"}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _valid_date(value):
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


# Contacts and touches: the text fields each may carry and their length caps,
# and the fields that come from a closed vocabulary.
CONTACT_TEXT_MAX_LEN = {"org": 200, "role_id": 200, "name": 200, "title": 200,
                        "url": 500, "email": 254, "source": 200, "notes": 1000}
TOUCH_TEXT_MAX_LEN = {"role_id": 200, "summary": 1000}
CONTACT_VOCAB = {"kind": CONTACT_KINDS}
TOUCH_VOCAB = {"channel": TOUCH_CHANNELS, "direction": TOUCH_DIRECTIONS}

APPLIED_STATUS = "03 Applied"
CLOSED_STATUS = "04 Closed"


def _after_write(store, role_id, field, value, result):
    """Knock-on writes for a field that just changed. Returns {field: value}
    for anything it wrote, for the response.

      - Moving a role to Applied starts its application record (date applied,
        stage) where those are empty.
      - Reopening a closed role clears its Close Reason: why you passed no
        longer holds, and a reason left on a live role would read as a
        preference. Outcomes stays - it may be text you wrote by hand.
    """
    if field != "status" or not result["changed"]:
        return {}
    written = {}
    if result["old"] == CLOSED_STATUS and value != CLOSED_STATUS:
        if store.set_field(role_id, "close_reason", None, actor="dashboard")["changed"]:
            written["close_reason"] = None
    if value == APPLIED_STATUS:
        written.update(store.fill_applied_defaults(role_id, actor="dashboard"))
    return written


# Ceiling on a single bulk write. Not a performance limit - a blast-radius one:
# an accidental "select all" on a tracker that has grown past this should be
# refused loudly rather than rewritten silently.
BULK_MAX_ROWS = 500


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
            role["notes"] = _clean(row.get("Notes"))
            role["comp_range"] = _clean(row.get("Range"))
            role["outcome"] = _clean(row.get("Outcomes"))
            role["close_reason"] = _clean(row.get("Close Reason"))
            role["role_function"] = _clean(row.get("Function"))
            role["date_posted"] = _clean(row.get("Date Posted")) or role.get("date_posted")
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


def render_profile_live(base):
    """The Profile page, fresh from the database, so triage shows on reload.
    None if it fails - the static copy on disk is served instead."""
    try:
        _path, html = _render_profile_page(_env(), write=False, base=Path(base))
        return html
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"live profile render failed, serving the static file: {exc}\n")
        return None


FINDING_NOTE_MAX_LEN = 500


def render_applications_live(base):
    """The Applications page, fresh from the database, so every save shows on
    reload. None if it fails - the static copy on disk is served instead."""
    try:
        _path, html = _render_applications_page(_env(), write=False, base=Path(base))
        return html
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"live applications render failed, serving the static file: {exc}\n")
        return None


def render_settings_live(base):
    """The Settings page, read fresh from the files it edits. None if it fails."""
    try:
        _path, html = _render_settings_page(_env(), write=False, base=Path(base))
        return html
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"live settings render failed, serving the static file: {exc}\n")
        return None


def _role_cat_options(base, store):
    try:
        return [""] + [a["label"] for a in load_archetypes(base, store.to_df())]
    except Exception:  # noqa: BLE001 - a missing me/profile.md must not break serving
        return [""]


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
        # Per-instance, not a module-level constant: two servers can run in
        # the same process (tests do this) pointed at different base dirs, and
        # each must serve its own artifacts/html, not whichever ran last.
        root = str(self.store.base_dir / "artifacts/html")
        super().__init__(*args, directory=root, **kwargs)

    # ---------- helpers ----------

    def _loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def end_headers(self):
        # Static assets (theme.css above all) must be revalidated, never reused
        # blind: the pages are regenerated constantly, and a stale stylesheet is
        # invisible - the page looks fine, it is just wearing yesterday's CSS.
        # Responses that already set the header (the JSON API says no-store)
        # keep their own value.
        sent = b"".join(self._headers_buffer or []).lower()
        if b"cache-control:" not in sent:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _validate(self, field, value):
        """(coerced_value, error). One gate for both the single-row and the
        bulk route, so a batch can never write something a single row could
        not: same vocabulary, same length caps, same type coercion."""
        if field not in self.editable:
            return value, f"field not editable: {field}"
        allowed = self.editable[field]
        # Validate against the vocabulary as text (a select element only ever
        # sends strings), then store the type the rest of the pipeline expects.
        if allowed is not None and str(value or "") not in allowed:
            return value, f"value not allowed for {field}: {value!r}"
        if allowed is None:
            # Free text (notes, comp_range/Salary): no vocabulary, just a length cap.
            if not isinstance(value, (str, type(None))):
                return value, f"{field} must be text"
            limit = FREE_TEXT_MAX_LEN.get(field)
            if limit and value and len(value) > limit:
                return value, f"{field} is too long (max {limit} characters)"
        if field in DATE_FIELDS and value and not _valid_date(value):
            return value, f"{field} must be a date, YYYY-MM-DD"
        if field == "priority" and value not in (None, ""):
            value = float(value)
        return value, None

    @staticmethod
    def _validate_record(payload, text_caps, vocab, extra=()):
        """(clean fields, error) for a contact or touch payload. Every field
        must be known; text is capped; vocabulary fields must be blank or in
        their list. `extra` names fields the caller checks itself."""
        unknown = set(payload) - set(text_caps) - set(vocab) - set(extra)
        if unknown:
            return None, f"unknown field(s): {', '.join(sorted(unknown))}"
        clean = {}
        for field, value in payload.items():
            if field in extra:
                continue
            if value is not None and not isinstance(value, str):
                return None, f"{field} must be text"
            if field in vocab and (value or "") not in [""] + vocab[field]:
                return None, f"value not allowed for {field}: {value!r}"
            limit = text_caps.get(field)
            if limit and value and len(value) > limit:
                return None, f"{field} is too long (max {limit} characters)"
            clean[field] = value or None
        return clean, None

    def _validate_contact(self, payload, creating):
        clean, error = self._validate_record(payload, CONTACT_TEXT_MAX_LEN, CONTACT_VOCAB,
                                             extra=() if creating else ("archived",))
        if error:
            return None, error
        if creating and not (clean.get("org") and clean.get("name")):
            return None, "a contact needs an org and a name"
        url = clean.get("url")
        if url and not re.match(r"https?://", url, re.I):
            return None, "url must start with http:// or https://"
        email = clean.get("email")
        if email and (" " in email or "@" not in email):
            return None, "email does not look like an address"
        if "archived" in payload:
            if not isinstance(payload["archived"], bool):
                return None, "archived must be true or false"
            clean["archived"] = payload["archived"]
        return clean, None

    def _validate_touch(self, payload):
        clean, error = self._validate_record(payload, TOUCH_TEXT_MAX_LEN, TOUCH_VOCAB,
                                             extra=("date", "contact_id"))
        if error:
            return None, error
        if not clean.get("role_id"):
            return None, "a touch needs a role_id"
        if not _valid_date(payload.get("date")):
            return None, "date must be a date, YYYY-MM-DD"
        clean["date"] = payload["date"]
        contact_id = payload.get("contact_id")
        if contact_id is not None:
            if isinstance(contact_id, bool) or not isinstance(contact_id, int):
                return None, "contact_id must be a number"
            clean["contact_id"] = contact_id
        return clean, None

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write(f"[{datetime.now():%H:%M:%S}] {fmt % args}\n")

    # ---------- routes ----------

    def _html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/profile.html":
            html = render_profile_live(self.store.base_dir)
            if html is not None:
                return self._html(html)
        if path == "/settings.html":
            html = render_settings_live(self.store.base_dir)
            if html is not None:
                return self._html(html)
        if path == "/applications.html":
            html = render_applications_live(self.store.base_dir)
            if html is not None:
                return self._html(html)
        if path == "/api/settings":
            if not self._loopback():
                return self._json({"error": "loopback only"}, 403)
            return self._json(profile_settings.read_all(self.store.base_dir))
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
                # Kept apart from `editable`, which names ROLE fields: a finding's
                # state is written through /api/finding/<id>, never /api/role/.
                "finding_states": list(Store.FINDING_STATES),
                "date_fields": sorted(DATE_FIELDS),
                "vocab": {"contact_kind": CONTACT_KINDS, "touch_channel": TOUCH_CHANNELS,
                          "touch_direction": TOUCH_DIRECTIONS},
                # relative to this server's own base, not the module-level BASE
                # constant - those differ for a temp-dir store such as a test.
                "db": str(self.store.db_path.relative_to(self.store.base_dir)),
                # Compare against `git rev-parse --short HEAD` if the dashboard
                # seems to be missing a feature you know shipped: a mismatch
                # means this process needs restarting to pick up the change.
                "server_commit": SERVER_COMMIT,
                "server_started_at": SERVER_STARTED_AT,
            })
        if path == "/api/changes":
            if not self._loopback():
                return self._json({"error": "loopback only"}, 403)
            return self._json({"changes": self.store.history(limit=50)})
        if path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        return super().do_GET()

    # POST routes. Exact paths first, then the patterned ones; the role route
    # is last so /api/role/<id>/close is not read as a role id.
    _POST_EXACT = {
        "/api/roles/bulk": "_bulk",
        "/api/contacts": "_create_contact",
        "/api/touches": "_create_touch",
        "/api/persona": "_persona",
        "/api/settings": "_settings",
    }
    _POST_PATTERNS = [
        (re.compile(r"/api/finding/([^/]+)/?"), "_finding"),
        (re.compile(r"/api/role/([^/]+)/close/?"), "_close_role"),
        (re.compile(r"/api/contact/(\d+)/?"), "_update_contact"),
        (re.compile(r"/api/touch/(\d+)/?"), "_update_touch"),
        (re.compile(r"/api/role/([^/]+)/?"), "_set_role_field"),
    ]

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._loopback():
            return self._json({"error": "loopback only"}, 403)

        handler, args = self._POST_EXACT.get(path), ()
        if handler is None:
            for pattern, name in self._POST_PATTERNS:
                match = pattern.fullmatch(path)
                if match:
                    handler, args = name, match.groups()
                    break
        if handler is None:
            return self._json({"error": "not found"}, 404)

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad json"}, 400)
        if not isinstance(payload, dict):
            return self._json({"error": "body must be a json object"}, 400)

        return getattr(self, handler)(payload, *args)

    def _set_role_field(self, payload, role_id):
        field = payload.get("field")
        value = payload.get("value")
        value, error = self._validate(field, value)
        if error:
            return self._json({"error": error}, 400)

        try:
            with _write_lock:
                result = self.store.set_field(role_id, field, value or None, actor="dashboard")
                filled = _after_write(self.store, role_id, field, value, result)
                confirmed = field in CONFIRMABLE and value and not result["changed"]
                if confirmed:
                    self.store.confirm_field(role_id, field, actor="dashboard")
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
            "filled": filled,
            "confirmed": bool(confirmed),
        })

    def _finding(self, payload, raw_id):
        """Your triage of one Profile finding: {state, note}."""
        try:
            finding_id = int(raw_id)
        except ValueError:
            return self._json({"error": f"unknown finding: {raw_id}"}, 404)
        state, note = payload.get("state"), payload.get("note")
        if state not in Store.FINDING_STATES:
            return self._json({"error": f"state must be one of {', '.join(Store.FINDING_STATES)}"}, 400)
        if note is not None and not isinstance(note, str):
            return self._json({"error": "note must be text"}, 400)
        if note and len(note) > FINDING_NOTE_MAX_LEN:
            return self._json({"error": f"note is too long (max {FINDING_NOTE_MAX_LEN} characters)"}, 400)
        try:
            with _write_lock:
                result = self.store.set_finding_state(finding_id, state, note, actor="dashboard")
        except KeyError:
            return self._json({"error": f"unknown finding: {finding_id}"}, 404)
        return self._json({"ok": True, "id": finding_id, **result})

    def _persona(self, payload):
        """Approve the text for one profile field into me/persona.md: {field, text}.
        Only fields the latest evaluation drafted, within their platform limit."""
        from profile_drafts import approve_field

        ev = self.store.latest_evaluation()
        limits = {d["key"]: d["limit"] for d in ((ev or {}).get("drafts") or [])}
        field, text = payload.get("field"), payload.get("text")
        if field not in limits:
            return self._json({"error": f"not a drafted field: {field!r}"}, 400)
        if not isinstance(text, str) or not text.strip():
            return self._json({"error": "text must be non-empty"}, 400)
        if len(text.strip()) > limits[field]:
            return self._json({"error": f"text is {len(text.strip())} characters; "
                                        f"the limit is {limits[field]}"}, 400)
        with _write_lock:
            old, new = approve_field(self.store.base_dir, field, text)
            self.store.log_change(f"persona:{field}", old, new, actor="dashboard")
        return self._json({"ok": True, "field": field, "changed": old != new})

    def _settings(self, payload):
        """One profile setting: {group, field, value}. group is goals (a section
        of me/profile.md), accounts (the workspace .env identity) or review
        (the workspace's config/profile.yaml)."""
        group, field, value = payload.get("group"), payload.get("field"), payload.get("value")
        base = self.store.base_dir
        try:
            with _write_lock:
                old, new = profile_settings.set_field(base, group, field, value)
                if old != new:
                    self.store.log_change(f"settings:{group}.{field}", json.dumps(old),
                                          json.dumps(new), actor="dashboard")
                if group == "goals" and field == "target_roles":
                    # Role Cat's vocabulary is the numbered Target Roles list, so
                    # it follows the edit without a restart.
                    self.editable["role_cat"] = _role_cat_options(base, self.store)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "group": group, "field": field,
                           "changed": old != new, "value": new})

    def _close_role(self, payload, role_id):
        """Close a role in one action: status to Closed, plus what the employer
        did (Outcomes) and why you passed (Close Reason), either of which may
        be blank. Blank leaves the field as it was - it never erases an outcome
        you wrote by hand. Each write is its own logged set_field, like bulk."""
        unknown = set(payload) - {"outcome", "close_reason"}
        if unknown:
            return self._json({"error": f"unknown field(s): {', '.join(sorted(unknown))}"}, 400)
        writes = [("status", CLOSED_STATUS)]
        for key, field in (("outcome", "outcomes"), ("close_reason", "close_reason")):
            value = payload.get(key)
            value, error = self._validate(field, value)
            if error:
                return self._json({"error": error}, 400)
            if value:
                writes.append((field, value))

        results = {}
        try:
            with _write_lock:
                if self.store.get_role(role_id) is None:
                    raise KeyError(role_id)
                for field, value in writes:
                    results[field] = self.store.set_field(role_id, field, value, actor="dashboard")
        except KeyError:
            return self._json({"error": f"unknown role: {role_id}"}, 404)
        return self._json({"ok": True, "id": role_id, "changes": results})

    def _create_contact(self, payload):
        clean, error = self._validate_contact(payload, creating=True)
        if error:
            return self._json({"error": error}, 400)
        try:
            with _write_lock:
                contact = self.store.add_contact(actor="dashboard", **clean)
        except KeyError:
            return self._json({"error": f"unknown role: {clean.get('role_id')}"}, 404)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "contact": contact})

    def _update_contact(self, payload, contact_id):
        clean, error = self._validate_contact(payload, creating=False)
        if error:
            return self._json({"error": error}, 400)
        if not clean:
            return self._json({"error": "nothing to update"}, 400)
        try:
            with _write_lock:
                if self.store.get_contact(int(contact_id)) is None:
                    return self._json({"error": f"unknown contact: {contact_id}"}, 404)
                result = self.store.update_contact(int(contact_id), actor="dashboard", **clean)
        except KeyError:
            return self._json({"error": f"unknown role: {clean.get('role_id')}"}, 404)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, **result})

    def _create_touch(self, payload):
        clean, error = self._validate_touch(payload)
        if error:
            return self._json({"error": error}, 400)
        try:
            with _write_lock:
                touch = self.store.add_touch(actor="dashboard", **clean)
        except KeyError as exc:
            return self._json({"error": f"unknown role or contact: {exc.args[0]}"}, 404)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "touch": touch})

    def _update_touch(self, payload, touch_id):
        """Touches are only ever archived - to fix one, archive it and log it
        again, so the log keeps what was first recorded."""
        if payload != {"archived": True}:
            return self._json({"error": 'the only update to a touch is {"archived": true}'}, 400)
        try:
            with _write_lock:
                result = self.store.archive_touch(int(touch_id), actor="dashboard")
        except KeyError:
            return self._json({"error": f"unknown touch: {touch_id}"}, 404)
        return self._json({"ok": True, **result})

    def _bulk(self, payload):
        """One field, one value, many rows - the batch behind the dashboard's
        "apply to selected" bar (moving every recommended skip to Closed in one
        action, for instance).

        Deliberately not a transaction: each row is an independent
        store.set_field, logged individually, so a bad id in the middle stops
        nothing and the change log reads the same as if the rows had been
        edited one at a time. The response reports every row's outcome, so the
        page can flash the ones that failed.
        """
        ids = payload.get("ids")
        field = payload.get("field")
        value = payload.get("value")

        if not isinstance(ids, list) or not ids:
            return self._json({"error": "ids must be a non-empty list"}, 400)
        if len(ids) > BULK_MAX_ROWS:
            return self._json({"error": f"too many rows: {len(ids)} (max {BULK_MAX_ROWS})"}, 400)
        if not all(isinstance(i, str) and i.strip() for i in ids):
            return self._json({"error": "ids must be non-empty strings"}, 400)

        value, error = self._validate(field, value)
        if error:
            return self._json({"error": error}, 400)

        results, changed, failed = [], 0, 0
        with _write_lock:
            for role_id in dict.fromkeys(ids):     # de-duplicated, order kept
                try:
                    outcome = self.store.set_field(role_id, field, value or None, actor="dashboard")
                    filled = _after_write(self.store, role_id, field, value, outcome)
                except KeyError:
                    results.append({"id": role_id, "error": f"unknown role: {role_id}"})
                    failed += 1
                    continue
                except ValueError as exc:
                    results.append({"id": role_id, "error": str(exc)})
                    failed += 1
                    continue
                changed += 1 if outcome["changed"] else 0
                results.append({"id": role_id, "changed": outcome["changed"],
                                "old": outcome["old"], "new": outcome["new"],
                                "filled": filled})

        return self._json({
            "ok": failed == 0,
            "field": field,
            "requested": len(results),
            "changed": changed,
            "unchanged": len(results) - changed - failed,
            "failed": failed,
            "results": results,
        })


def serve(host="127.0.0.1", port=8787, base=None, quiet=False):
    base = Path(base) if base else workspace.resolve()
    store = Store(base)
    editable = dict(EDITABLE)
    editable["outcomes"] = _outcome_options()
    editable["role_cat"] = _role_cat_options(base, store)

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
    workspace.add_argument(parser)
    args = parser.parse_args()
    base = workspace.resolve(args.user)

    if not any((base / "artifacts/html").glob("*.html")):
        print(f"nothing rendered yet for workspace {base.name!r} - run: "
              f"uv run render/build.py --user {base.name}")
        return 1

    httpd = serve(args.host, args.port, base=base)
    store = httpd.mc_store
    print(f"Mission Control writeback server")
    print(f"  dashboard : http://{args.host}:{args.port}/job-tracker.html")
    print(f"  settings  : http://{args.host}:{args.port}/settings.html")
    print(f"  workspace : {base}")
    print(f"  database  : {store.db_path.relative_to(base)} ({store.count()} roles)")
    print(f"  editable  : {', '.join(httpd.mc_editable)}")
    print(f"  commit    : {SERVER_COMMIT}  (restart this process after any code change - "
          f"Python does not hot-reload)")
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

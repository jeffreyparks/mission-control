"""
SQLite store for Mission Control.

The database at data/mission-control.db is the only source of record.

Durability, since one SQLite file now holds everything:
  - backup_db() takes a rotating, WAL-safe copy of the database before each run.
  - export_csv() writes a rotating plain-text copy of the tracker, readable
    without this tool - or any tool.
  - Every write goes through a change log, so any field can be traced back.

An Excel tracker used to be written on every change and imported back at the
start of each run, so edits made in the spreadsheet were not lost. The dashboard
owns those edits now, so the round trip - and the spreadsheet - are gone.

Nothing here imports the rest of the pipeline, so it can be used from the daily
run, from a writeback server, or from a REPL.
"""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_NAME = "data/mission-control.db"

# Rotating copies kept for the database and the CSV export.
KEEP_BACKUPS = 7

# Display header -> db column. The headers the tracker, the CSV export and the
# dashboard all use; add a column here and every surface follows.
COLUMN_MAP = {
    "Org": "org",
    "Title": "title",
    "Role Cat": "role_cat",
    "Priority": "priority",
    "Date Opened": "date_opened",
    "Date Applied": "date_applied",
    "Date Posted": "date_posted",
    "Status": "status",
    "Outcomes": "outcomes",
    "Source": "source",
    "Match Score": "match_score",
    "Keywords Matched": "keywords_matched",
    "Role Link": "role_link",
    "Range": "comp_range",
    "Notes": "notes",
    "Other Links": "other_links",
    "Last Updated": "last_updated",
    "Fit Score": "fit_score",
    "Fit Rationale": "fit_rationale",
    "Recommendation": "recommendation",
    "Sector": "sector",
    "Function": "role_function",
    "Days To Outcome": "days_to_outcome",
    "Role Cat (suggested)": "role_cat_suggested",
    "Close Reason": "close_reason",
    "Stage": "stage",
    "Next Action": "next_action",
    "Next Action Due": "next_action_due",
}
DB_TO_HEADER = {v: k for k, v in COLUMN_MAP.items()}

# Columns a human owns. The pipeline never overwrites these.
MANUAL_FIELDS = ("role_cat", "priority", "status", "outcomes", "notes",
                 "date_applied", "comp_range", "other_links", "close_reason",
                 "stage", "next_action", "next_action_due")

# Why YOU passed on a role - the user's side of a close, kept apart from
# Outcomes, which records what the employer did. The split matters to anything
# that learns from decisions: "chose another role at the org" or "posting
# closed" says nothing about what you want, while "not a fit - function" does.
CLOSE_REASONS = [
    "Not a fit - function",
    "Not a fit - level",
    "Not a fit - skills gap",
    "Not a fit - industry",
    "Comp",
    "Location",
    "Chose another role at org",
    "Posting closed",
    "Other",
]

# The subset that expresses a preference about the role itself.
PREFERENCE_CLOSE_REASONS = frozenset(CLOSE_REASONS[:6])

# Where an applied role has got to. Kept apart from Status on purpose: status
# says whether a role is live at all, and everything downstream (the tracker,
# the preferences learner) keys off its five values. Stage only means anything
# while a role is "03 Applied".
STAGES = ["Applied", "Screen", "Interviewing", "Final", "Offer"]

# Vocabularies for the Applications tab's contacts and touches. The store
# records whatever it is given; the dashboard is the gate that enforces these.
CONTACT_KINDS = ["recruiter", "hiring_manager", "referrer", "peer", "other"]
TOUCH_CHANNELS = ["email", "linkedin", "call", "interview", "portal", "other"]
TOUCH_DIRECTIONS = ["out", "in"]

CONTACT_FIELDS = ("org", "role_id", "name", "title", "kind", "url", "email",
                  "source", "notes")
TOUCH_FIELDS = ("role_id", "contact_id", "date", "channel", "direction", "summary")

SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    id                  TEXT PRIMARY KEY,
    row_order           INTEGER,
    org                 TEXT,
    title               TEXT,
    role_cat            TEXT,
    priority            REAL,
    date_opened         TEXT,
    date_applied        TEXT,
    date_posted         TEXT,
    status              TEXT,
    outcomes            TEXT,
    source              TEXT,
    match_score         REAL,
    keywords_matched    TEXT,
    role_link           TEXT,
    comp_range          TEXT,
    notes               TEXT,
    other_links         TEXT,
    last_updated        TEXT,
    fit_score           INTEGER,
    fit_rationale       TEXT,
    recommendation      TEXT,
    sector              TEXT,
    role_function       TEXT,
    days_to_outcome     REAL,
    role_cat_suggested  TEXT,
    close_reason        TEXT,
    stage               TEXT,
    next_action         TEXT,
    next_action_due     TEXT,
    created_at          TEXT,
    updated_at          TEXT
);

-- People at an org: recruiters, hiring managers, referrers. Keyed to the org
-- rather than a role, since one recruiter often covers several roles there;
-- role_id is set only when a contact is about one role in particular.
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org         TEXT NOT NULL,
    role_id     TEXT,
    name        TEXT NOT NULL,
    title       TEXT,
    kind        TEXT,
    url         TEXT,
    email       TEXT,
    source      TEXT,
    notes       TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);

-- Every contact with an employer about a role, either way: an email sent, a
-- recruiter's reply, an interview. contact_id is empty for things like
-- "submitted via the portal".
CREATE TABLE IF NOT EXISTS touches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id     TEXT NOT NULL,
    contact_id  INTEGER,
    date        TEXT NOT NULL,
    channel     TEXT,
    direction   TEXT,
    summary     TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id    TEXT,
    field      TEXT,
    old_value  TEXT,
    new_value  TEXT,
    actor      TEXT,
    ts         TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_changes_role ON changes(role_id);
CREATE INDEX IF NOT EXISTS idx_roles_status ON roles(status);
CREATE INDEX IF NOT EXISTS idx_touches_role ON touches(role_id);
"""


def _slug(org, title):
    raw = f"{org}--{title}".lower()
    return re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:120]


def _norm(value):
    """Spreadsheet-style blanks and sqlite NULLs: one representation, None."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d")
    return value


def _same(a, b):
    """Compare as the user sees them: 1 and 1.0 are the same priority."""
    a, b = _norm(a), _norm(b)
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a).strip() == str(b).strip()


class Store:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.db_path = self.base_dir / DB_NAME
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    # ---------- plumbing ----------

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self):
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._add_missing_columns(conn)

    @staticmethod
    def _add_missing_columns(conn):
        """CREATE TABLE IF NOT EXISTS leaves an older database's table as it
        was, so a column added to COLUMN_MAP since would be missing from it.
        Add any such column, empty. Additive only - nothing is dropped or
        rewritten, and the change is idempotent."""
        have = {row["name"] for row in conn.execute("PRAGMA table_info(roles)")}
        for field in COLUMN_MAP.values():
            if field not in have:
                conn.execute(f"ALTER TABLE roles ADD COLUMN {field} TEXT")

    def get_meta(self, key, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def count(self):
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) n FROM roles").fetchone()["n"]

    def is_empty(self):
        return self.count() == 0

    # ---------- ids ----------

    def _assign_ids(self, frame):
        """Stable id per row. Two rows can legitimately share org+title (the same
        role posted twice), so collisions get a numeric suffix in row order."""
        ids, seen = [], {}
        for _, row in frame.iterrows():
            base = _slug(_norm(row.get("Org")) or "Unknown",
                         _norm(row.get("Title")) or "Untitled")
            seen[base] = seen.get(base, 0) + 1
            ids.append(base if seen[base] == 1 else f"{base}--{seen[base]}")
        return ids

    # ---------- reads ----------

    def to_df(self):
        """The tracker as the pipeline expects it: display headers, display order."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM roles ORDER BY row_order, rowid").fetchall()
        records = []
        for row in rows:
            record = {header: row[field] for field, header in DB_TO_HEADER.items()}
            record["_id"] = row["id"]
            records.append(record)
        columns = list(COLUMN_MAP.keys()) + ["_id"]
        return pd.DataFrame(records, columns=columns) if records else pd.DataFrame(columns=columns)

    def history(self, role_id=None, limit=50):
        query = "SELECT * FROM changes"
        args = []
        if role_id:
            query += " WHERE role_id=?"
            args.append(role_id)
        query += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, args).fetchall()]

    # ---------- writes ----------

    def _log(self, conn, role_id, field, old, new, actor):
        conn.execute(
            "INSERT INTO changes(role_id,field,old_value,new_value,actor,ts) "
            "VALUES(?,?,?,?,?,?)",
            (role_id, field, None if old is None else str(old),
             None if new is None else str(new), actor, datetime.now().isoformat(timespec="seconds")),
        )

    def save_df(self, frame, actor="pipeline"):
        """Write a tracker DataFrame back. Rows are matched by _id when present,
        otherwise by generated id. Returns a summary of what moved."""
        frame = frame.copy()
        if "_id" not in frame.columns or frame["_id"].isna().any():
            generated = self._assign_ids(frame)
            if "_id" not in frame.columns:
                frame["_id"] = generated
            else:
                frame["_id"] = [
                    existing if _norm(existing) else generated[i]
                    for i, existing in enumerate(frame["_id"])
                ]

        now = datetime.now().isoformat(timespec="seconds")
        inserted, updated, changed_fields = 0, 0, 0

        with self.connect() as conn:
            existing = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM roles")}
            for order, (_, row) in enumerate(frame.iterrows()):
                role_id = _norm(row["_id"])
                values = {field: _norm(row.get(header)) for header, field in COLUMN_MAP.items()}
                current = existing.get(role_id)

                if current is None:
                    conn.execute(
                        f"INSERT INTO roles(id,row_order,created_at,updated_at,"
                        f"{','.join(values)}) VALUES(?,?,?,?,{','.join('?' * len(values))})",
                        [role_id, order, now, now, *values.values()],
                    )
                    self._log(conn, role_id, "*", None, "created", actor)
                    inserted += 1
                    continue

                deltas = {f: v for f, v in values.items() if not _same(current.get(f), v)}
                if not deltas and current.get("row_order") == order:
                    continue
                for field, value in deltas.items():
                    self._log(conn, role_id, field, current.get(field), value, actor)
                changed_fields += len(deltas)
                assignments = ",".join(f"{f}=?" for f in deltas)
                sql = "UPDATE roles SET row_order=?, updated_at=?"
                args = [order, now]
                if assignments:
                    sql += "," + assignments
                    args += list(deltas.values())
                sql += " WHERE id=?"
                args.append(role_id)
                conn.execute(sql, args)
                if deltas:
                    updated += 1

        return {"inserted": inserted, "updated": updated, "fields": changed_fields}

    def set_field(self, role_id, field, value, actor="dashboard"):
        """Single-field update. This is what the writeback server will call."""
        if field not in DB_TO_HEADER:
            raise ValueError(f"unknown field: {field}")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
            if row is None:
                raise KeyError(role_id)
            old = row[field]
            if _same(old, value):
                return {"changed": False, "old": old, "new": value}
            conn.execute(
                f"UPDATE roles SET {field}=?, updated_at=? WHERE id=?",
                (_norm(value), datetime.now().isoformat(timespec="seconds"), role_id),
            )
            self._log(conn, role_id, field, old, value, actor)
        return {"changed": True, "old": old, "new": _norm(value)}

    # ---------- contacts and touches ----------
    #
    # Same rules as roles: every write is logged to `changes`, and nothing is
    # deleted - archiving hides a row, the row and its history stay. Each log
    # entry is filed under the role it concerns (a contact's role_id may be
    # empty), with field "contact:<id>" or "touch:<id>" and the values as JSON.

    @staticmethod
    def _clean_fields(fields, allowed):
        unknown = set(fields) - set(allowed)
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        return {k: _norm(v) for k, v in fields.items()}

    @staticmethod
    def _require_role(conn, role_id):
        if conn.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone() is None:
            raise KeyError(role_id)

    def get_contact(self, contact_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        return dict(row) if row else None

    def add_contact(self, actor="dashboard", **fields):
        """A new contact. org and name are required; role_id, if given, must
        be a role that exists. Returns the stored row."""
        values = self._clean_fields(fields, CONTACT_FIELDS)
        if not values.get("org") or not values.get("name"):
            raise ValueError("a contact needs an org and a name")
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            if values.get("role_id"):
                self._require_role(conn, values["role_id"])
            cols = list(values)
            cur = conn.execute(
                f"INSERT INTO contacts({','.join(cols)},created_at,updated_at) "
                f"VALUES({','.join('?' * len(cols))},?,?)",
                [*values.values(), now, now],
            )
            contact_id = cur.lastrowid
            self._log(conn, values.get("role_id"), f"contact:{contact_id}", None,
                      json.dumps(values), actor)
        return self.get_contact(contact_id)

    def update_contact(self, contact_id, actor="dashboard", **fields):
        """Change some of a contact's fields. Only the fields that actually
        change are written and logged."""
        values = self._clean_fields(fields, CONTACT_FIELDS + ("archived",))
        if "archived" in values:
            values["archived"] = 1 if values["archived"] else 0
        for required in ("org", "name"):
            if required in values and not values[required]:
                raise ValueError(f"a contact's {required} cannot be empty")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
            if row is None:
                raise KeyError(contact_id)
            if values.get("role_id"):
                self._require_role(conn, values["role_id"])
            deltas = {f: v for f, v in values.items() if not _same(row[f], v)}
            if not deltas:
                return {"changed": False, "contact": dict(row)}
            conn.execute(
                f"UPDATE contacts SET {','.join(f'{f}=?' for f in deltas)}, updated_at=? WHERE id=?",
                [*deltas.values(), datetime.now().isoformat(timespec="seconds"), contact_id],
            )
            self._log(conn, deltas.get("role_id", row["role_id"]), f"contact:{contact_id}",
                      json.dumps({f: row[f] for f in deltas}), json.dumps(deltas), actor)
        return {"changed": True, "contact": self.get_contact(contact_id)}

    def archive_contact(self, contact_id, actor="dashboard"):
        return self.update_contact(contact_id, actor=actor, archived=True)

    def contacts_for(self, org, include_archived=False):
        """Everyone at an org, matched without regard to case or surrounding
        spaces, so a contact shows on every role there."""
        query = "SELECT * FROM contacts WHERE lower(trim(org)) = lower(trim(?))"
        if not include_archived:
            query += " AND archived = 0"
        with self.connect() as conn:
            rows = conn.execute(query + " ORDER BY name, id", (org or "",)).fetchall()
        return [dict(r) for r in rows]

    def get_touch(self, touch_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM touches WHERE id=?", (touch_id,)).fetchone()
        return dict(row) if row else None

    def add_touch(self, actor="dashboard", **fields):
        """A new touch. role_id and date are required and the role must exist;
        contact_id, if given, must be a contact that exists."""
        values = self._clean_fields(fields, TOUCH_FIELDS)
        if not values.get("role_id") or not values.get("date"):
            raise ValueError("a touch needs a role_id and a date")
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            self._require_role(conn, values["role_id"])
            if values.get("contact_id") is not None:
                found = conn.execute("SELECT 1 FROM contacts WHERE id=?",
                                     (values["contact_id"],)).fetchone()
                if found is None:
                    raise KeyError(values["contact_id"])
            cols = list(values)
            cur = conn.execute(
                f"INSERT INTO touches({','.join(cols)},created_at) "
                f"VALUES({','.join('?' * len(cols))},?)",
                [*values.values(), now],
            )
            touch_id = cur.lastrowid
            self._log(conn, values["role_id"], f"touch:{touch_id}", None,
                      json.dumps(values), actor)
        return self.get_touch(touch_id)

    def archive_touch(self, touch_id, actor="dashboard"):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM touches WHERE id=?", (touch_id,)).fetchone()
            if row is None:
                raise KeyError(touch_id)
            if row["archived"]:
                return {"changed": False, "touch": dict(row)}
            conn.execute("UPDATE touches SET archived=1 WHERE id=?", (touch_id,))
            self._log(conn, row["role_id"], f"touch:{touch_id}",
                      json.dumps({"archived": 0}), json.dumps({"archived": 1}), actor)
        return {"changed": True, "touch": self.get_touch(touch_id)}

    def touches_for(self, role_id, include_archived=False):
        """A role's touches, newest first."""
        query = "SELECT * FROM touches WHERE role_id=?"
        if not include_archived:
            query += " AND archived = 0"
        with self.connect() as conn:
            rows = conn.execute(query + " ORDER BY date DESC, id DESC", (role_id,)).fetchall()
        return [dict(r) for r in rows]


    def backup_db(self, keep=KEEP_BACKUPS):
        """Timestamped copy of the database, newest `keep` retained.

        Uses SQLite's own backup API rather than copying the file. The database
        runs in WAL mode, so a plain file copy can catch it mid-transaction and
        miss everything still in the write-ahead log - a backup that looks fine
        until the day it is needed.

        This is the real record. Until now the only thing backed up was the
        exported spreadsheet, which is derived from it."""
        dest_dir = self.db_path.parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"mission-control-{stamp}.db"

        src = sqlite3.connect(self.db_path)
        try:
            target = sqlite3.connect(dest)
            try:
                src.backup(target)
            finally:
                target.close()
        finally:
            src.close()

        self._prune(dest_dir.glob("mission-control-*.db"), keep, "database backup")
        return dest

    def export_csv(self, keep=KEEP_BACKUPS):
        """Timestamped CSV of the tracker, newest `keep` retained.

        Plain text, no dependency, opens in any spreadsheet - a copy of the
        record that outlives this tool and does not need it to be read back."""
        frame = self.to_df().drop(columns=["_id"], errors="ignore")
        out_dir = self.base_dir / "artifacts/jobs"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = out_dir / f"org-roles-tracker-{stamp}.csv"
        frame.to_csv(dest, index=False)
        self._prune(out_dir.glob("org-roles-tracker-*.csv"), keep, "csv export")
        self.set_meta("last_csv_export", datetime.now().isoformat(timespec="seconds"))
        return dest

    @staticmethod
    def _prune(paths, keep, label):
        """Delete all but the newest `keep` of a rotating set."""
        ordered = sorted(paths, reverse=True)
        dropped = []
        for path in ordered[keep:]:
            try:
                path.unlink()
                dropped.append(path.name)
            except OSError as exc:
                print(f"    ! could not remove {path.name}: {exc}")
        if dropped:
            print(f"  pruned {len(dropped)} old {label}(s), kept newest {keep}")
        return dropped

    def load(self, verbose=True):
        """Start-of-run entry point: the tracker as a DataFrame.

        The database is the only record now. This used to import the
        spreadsheet first, so hand edits made in Excel were not lost; the
        dashboard owns those edits today, and the spreadsheet is gone.
        """
        return self.to_df()

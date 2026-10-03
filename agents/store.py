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

-- What each public profile said, one row per CHANGE (agents/profile_snapshot.py).
-- A row is written only when `hash` differs from the latest row for that
-- source_key. Not to be confused with profile_snapshot in data/history.db,
-- which holds a daily LinkedIn keyword-coverage number for the home page.
CREATE TABLE IF NOT EXISTS profile_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key   TEXT NOT NULL,
    source       TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    hash         TEXT NOT NULL,
    payload      TEXT NOT NULL
);

-- One row per Profile evaluation (agents/profile_eval.py): what was judged
-- (snapshot ids, the target brief) and the result. Counts and shares are
-- computed in Python; only scores and wording come from a model.
CREATE TABLE IF NOT EXISTS profile_evaluations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    trigger       TEXT,
    snapshot_ids  TEXT NOT NULL,
    brief_hash    TEXT NOT NULL,
    brief         TEXT NOT NULL,
    scores        TEXT NOT NULL,
    shares        TEXT,
    per_source    TEXT,
    drafts        TEXT,
    summary       TEXT,
    model_calls   INTEGER,
    tokens_in     INTEGER,
    tokens_out    INTEGER
);

-- Findings live across evaluations, matched by fingerprint, so your triage
-- (accepted, dismissed) survives the next run. See sync_findings().
CREATE TABLE IF NOT EXISTS profile_findings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint    TEXT NOT NULL UNIQUE,
    evaluation_id  INTEGER NOT NULL,
    first_seen     TEXT NOT NULL,
    dimension      TEXT NOT NULL,
    source_key     TEXT,
    severity       TEXT NOT NULL,
    title          TEXT NOT NULL,
    quote          TEXT,
    url            TEXT,
    ask            TEXT,
    ask_roles      INTEGER,
    fix            TEXT,
    rank           INTEGER,
    came_back      INTEGER NOT NULL DEFAULT 0,
    state          TEXT NOT NULL DEFAULT 'open',
    state_note     TEXT,
    dismiss_note   TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_changes_role ON changes(role_id);
CREATE INDEX IF NOT EXISTS idx_roles_status ON roles(status);
CREATE INDEX IF NOT EXISTS idx_touches_role ON touches(role_id);
CREATE INDEX IF NOT EXISTS idx_profile_snapshots_key ON profile_snapshots(source_key, id);
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



def _source_changed(source_key, raised, current):
    """Has the profile a finding is about changed since it was raised?

    raised and current are {source_key: snapshot_id} for the evaluation that
    last raised the finding and for this one. A source not judged this time
    (its review failed) has not changed. A finding about no one source (all
    sources) changes when any judged source did."""
    if source_key:
        return source_key in current and current[source_key] != raised.get(source_key)
    return any(raised.get(k) != v for k, v in current.items())

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
            self._add_profile_columns(conn)

    @staticmethod
    def _add_profile_columns(conn):
        """Columns added to the profile tables after they first shipped. Additive
        and idempotent, like _add_missing_columns."""
        have = {row["name"] for row in conn.execute("PRAGMA table_info(profile_evaluations)")}
        if "drafts" not in have:
            conn.execute("ALTER TABLE profile_evaluations ADD COLUMN drafts TEXT")
        have = {row["name"] for row in conn.execute("PRAGMA table_info(profile_findings)")}
        if "dismiss_note" not in have:
            conn.execute("ALTER TABLE profile_findings ADD COLUMN dismiss_note TEXT")
            Store._backfill_dismiss_notes(conn)

    @staticmethod
    def _backfill_dismiss_notes(conn):
        """Recover the reason for each dismissal from the change log, where
        set_finding_state wrote it as "dismissed (<note>)". The log is the only
        copy for a finding that has since reopened."""
        notes = {}
        for row in conn.execute("SELECT field, new_value FROM changes WHERE field LIKE 'finding:%:state' "
                                "ORDER BY ts, seq"):
            value = row["new_value"] or ""
            if value.startswith("dismissed (") and value.endswith(")"):
                notes[row["field"].split(":")[1]] = value[len("dismissed ("):-1]
        for finding_id, note in notes.items():
            conn.execute("UPDATE profile_findings SET dismiss_note=? WHERE id=?", (note, finding_id))

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

    # ---------- profile snapshots ----------

    def latest_snapshot(self, source_key):
        """The most recent stored snapshot for one source, as a dict, or None."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, payload FROM profile_snapshots WHERE source_key=? "
                "ORDER BY id DESC LIMIT 1", (source_key,)).fetchone()
        if row is None:
            return None
        snap = json.loads(row["payload"])
        snap["_id"] = row["id"]
        return snap

    def latest_snapshots(self, source=None):
        """The newest snapshot for every source_key (optionally one source)."""
        query = ("SELECT p.id, p.payload FROM profile_snapshots p JOIN "
                 "(SELECT source_key, MAX(id) id FROM profile_snapshots GROUP BY source_key) m "
                 "ON p.id = m.id")
        params = ()
        if source:
            query += " WHERE p.source=?"
            params = (source,)
        with self.connect() as conn:
            rows = conn.execute(query + " ORDER BY p.source_key", params).fetchall()
        return [{**json.loads(r["payload"]), "_id": r["id"]} for r in rows]

    def save_snapshot_if_changed(self, snap):
        """Store `snap` unless its hash matches the latest for its source_key.

        Returns (changed, snapshot_id): the new row's id, or the unchanged
        latest row's. Either way the check time is recorded in meta, so "last
        checked" is known even for a profile that has not changed in months.
        """
        latest = self.latest_snapshot(snap["source_key"])
        self.set_meta(f"profile_checked:{snap['source_key']}", snap["captured_at"])
        if latest and latest.get("hash") == snap["hash"]:
            return False, latest["_id"]
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO profile_snapshots(source_key, source, captured_at, hash, payload) "
                "VALUES (?,?,?,?,?)",
                (snap["source_key"], snap["source"], snap["captured_at"], snap["hash"],
                 json.dumps(snap, ensure_ascii=False, default=str)),
            )
            return True, cur.lastrowid

    # ---------- profile evaluations and findings ----------

    _EVAL_JSON = ("snapshot_ids", "brief", "scores", "shares", "per_source", "drafts")

    def add_evaluation(self, record):
        """Store one evaluation. JSON-shaped fields are serialised here."""
        values = {k: (json.dumps(v, ensure_ascii=False, default=str) if k in self._EVAL_JSON else v)
                  for k, v in record.items()}
        cols = list(values)
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT INTO profile_evaluations({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})", list(values.values()))
            return cur.lastrowid

    def latest_evaluation(self):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM profile_evaluations ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        out = dict(row)
        for key in self._EVAL_JSON:
            out[key] = json.loads(out[key]) if out.get(key) else None
        return out

    def sync_findings(self, evaluation_id, findings, now=None):
        """Merge one evaluation's findings into the standing list.

          - a finding seen before (same fingerprint) keeps its state, and takes
            the new wording, severity and rank
          - a dismissed finding stays dismissed; it only reopens if its
            severity has risen, and keeps your reason in dismiss_note
          - an open or accepted finding NOT raised this time becomes fixed -
            but only when its source has changed since the finding was last
            raised. The reviewer's wording varies run to run; with the profile
            the same, a finding it skipped is not fixed, just not mentioned,
            and stays as it was ("held")
          - a fixed finding raised again reopens, marked came_back

        Returns {"new": n, "kept": n, "fixed": n, "reopened": n, "held": n}.
        """
        now = now or datetime.now().isoformat(timespec="seconds")
        rank_of = {"low": 0, "medium": 1, "high": 2}
        counts = {"new": 0, "kept": 0, "fixed": 0, "reopened": 0, "held": 0}
        seen = set()
        fields = ("dimension", "source_key", "severity", "title", "quote", "url", "ask",
                  "ask_roles", "fix", "rank")
        with self.connect() as conn:
            for f in findings:
                seen.add(f["fingerprint"])
                row = conn.execute("SELECT * FROM profile_findings WHERE fingerprint=?",
                                   (f["fingerprint"],)).fetchone()
                values = [f.get(k) for k in fields]
                if row is None:
                    conn.execute(
                        f"INSERT INTO profile_findings(fingerprint, evaluation_id, first_seen, "
                        f"{','.join(fields)}) VALUES (?,?,?,{','.join('?' * len(fields))})",
                        [f["fingerprint"], evaluation_id, now, *values])
                    counts["new"] += 1
                    continue
                state, came_back, note = row["state"], row["came_back"], row["state_note"]
                if state == "fixed":
                    state, came_back, note = "open", 1, None
                    counts["reopened"] += 1
                elif state == "dismissed" and rank_of.get(f["severity"], 0) > rank_of.get(row["severity"], 0):
                    state, note = "open", f"reopened: severity rose to {f['severity']}"
                    counts["reopened"] += 1
                else:
                    counts["kept"] += 1
                conn.execute(
                    f"UPDATE profile_findings SET evaluation_id=?, state=?, came_back=?, state_note=?, "
                    f"{','.join(f'{k}=?' for k in fields)} WHERE id=?",
                    [evaluation_id, state, came_back, note, *values, row["id"]])
            snapshots = {r["id"]: json.loads(r["snapshot_ids"] or "{}") for r in conn.execute(
                "SELECT id, snapshot_ids FROM profile_evaluations")}
            current = snapshots.get(evaluation_id, {})
            for row in conn.execute(
                    "SELECT id, fingerprint, source_key, evaluation_id FROM profile_findings "
                    "WHERE state IN ('open','accepted')").fetchall():
                if row["fingerprint"] in seen:
                    continue
                if not _source_changed(row["source_key"], snapshots.get(row["evaluation_id"], {}), current):
                    counts["held"] += 1
                    continue
                conn.execute("UPDATE profile_findings SET state='fixed', state_note=? WHERE id=?",
                             (f"resolved by evaluation {evaluation_id}", row["id"]))
                counts["fixed"] += 1
        return counts

    def set_evaluation_drafts(self, evaluation_id, drafts):
        with self.connect() as conn:
            conn.execute("UPDATE profile_evaluations SET drafts=? WHERE id=?",
                         (json.dumps(drafts, ensure_ascii=False), evaluation_id))

    def log_change(self, field, old, new, actor="dashboard"):
        """A change-log line for something that is not a role field (approved
        profile text, for instance), so every edit stays traceable."""
        with self.connect() as conn:
            self._log(conn, None, field, old, new, actor)

    def previous_evaluation(self):
        """The evaluation before the latest, for showing how scores moved."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM profile_evaluations ORDER BY id DESC LIMIT 1 OFFSET 1").fetchone()
        if row is None:
            return None
        out = dict(row)
        for key in self._EVAL_JSON:
            out[key] = json.loads(out[key]) if out.get(key) else None
        return out

    def get_snapshot(self, snapshot_id):
        with self.connect() as conn:
            row = conn.execute("SELECT id, payload FROM profile_snapshots WHERE id=?",
                               (snapshot_id,)).fetchone()
        return {**json.loads(row["payload"]), "_id": row["id"]} if row else None

    FINDING_STATES = ("open", "accepted", "dismissed", "fixed")

    def set_finding_state(self, finding_id, state, note=None, actor="dashboard"):
        """Your triage of one finding. Logged in the change log like tracker
        edits. Returns {"changed", "old", "new"}; KeyError for an unknown id."""
        if state not in self.FINDING_STATES:
            raise ValueError(f"unknown finding state: {state!r}")
        note = _norm(note)
        with self.connect() as conn:
            row = conn.execute("SELECT state, state_note FROM profile_findings WHERE id=?",
                               (finding_id,)).fetchone()
            if row is None:
                raise KeyError(finding_id)
            if row["state"] == state and (row["state_note"] or None) == note:
                return {"changed": False, "old": row["state"], "new": state}
            # dismiss_note is the reason you gave for dismissing, kept apart from
            # state_note so reopening cannot erase it - profile_guidance.py learns
            # from it. Dismissing again without a reason keeps the earlier one.
            conn.execute("UPDATE profile_findings SET state=?, state_note=?, "
                         "dismiss_note=CASE WHEN ? AND ? IS NOT NULL THEN ? ELSE dismiss_note END "
                         "WHERE id=?",
                         (state, note, state == "dismissed", note, note, finding_id))
            self._log(conn, None, f"finding:{finding_id}:state", row["state"],
                      state + (f" ({note})" if note else ""), actor)
        return {"changed": True, "old": row["state"], "new": state}

    def profile_findings(self, states=("open", "accepted")):
        query = "SELECT * FROM profile_findings"
        params = ()
        if states:
            query += f" WHERE state IN ({','.join('?' * len(states))})"
            params = tuple(states)
        order = (" ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
                 " COALESCE(ask_roles, 0) DESC, rank")
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query + order, params)]

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

    def get_role(self, role_id):
        """One roles row as a dict, db column names, or None."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
        return dict(row) if row else None

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

    def confirm_field(self, role_id, field, actor="dashboard"):
        """Record that a person checked a field and kept its value. set_field
        logs nothing when the value does not change, but "I looked, and this
        date is right" is information: it is what turns an estimated Date
        Applied into a confirmed one. Logged with old == new."""
        if field not in DB_TO_HEADER:
            raise ValueError(f"unknown field: {field}")
        with self.connect() as conn:
            row = conn.execute(f"SELECT {field} FROM roles WHERE id=?", (role_id,)).fetchone()
            if row is None:
                raise KeyError(role_id)
            self._log(conn, role_id, field, row[field], row[field], actor)
        return row[field]

    def fill_applied_defaults(self, role_id, today=None, actor="dashboard"):
        """When a role moves to "03 Applied", start its application record:
        date_applied is today and stage is "Applied" - each only if empty, so
        re-opening a role never rewrites the date you first applied. Returns
        {field: value} for whatever was filled."""
        today = today or datetime.now().strftime("%Y-%m-%d")
        with self.connect() as conn:
            row = conn.execute("SELECT date_applied, stage FROM roles WHERE id=?",
                               (role_id,)).fetchone()
        if row is None:
            raise KeyError(role_id)
        filled = {}
        for field, value in (("date_applied", today), ("stage", STAGES[0])):
            if _norm(row[field]) is None:
                self.set_field(role_id, field, value, actor=actor)
                filled[field] = value
        return filled

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

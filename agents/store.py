"""
SQLite store for Mission Control.

The database at data/mission-control.db is the source of record. The Excel
tracker is still written on every run, so the spreadsheet stays a real physical
backup you can open, mail, or restore from - it just stops being the thing the
pipeline depends on.

Two-way safety:
  - Hand edits to the xlsx are NOT lost. If the file changed since the last
    export, load() imports it before the run and logs every field it changed.
  - Every write goes through a change log, so any field can be traced back.

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

# xlsx header  ->  db column. This mapping is the contract between the two
# formats; add a column here and both sides follow.
COLUMN_MAP = {
    "Org": "org",
    "Title": "title",
    "Role Cat": "role_cat",
    "Priority": "priority",
    "Date Opened": "date_opened",
    "Date Applied": "date_applied",
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
    "Days To Outcome": "days_to_outcome",
    "Role Cat (suggested)": "role_cat_suggested",
}
DB_TO_XLSX = {v: k for k, v in COLUMN_MAP.items()}

# Columns a human owns. The pipeline never overwrites these.
MANUAL_FIELDS = ("role_cat", "priority", "status", "outcomes", "notes",
                 "date_applied", "comp_range", "other_links")

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
    days_to_outcome     REAL,
    role_cat_suggested  TEXT,
    created_at          TEXT,
    updated_at          TEXT
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
"""


def _slug(org, title):
    raw = f"{org}--{title}".lower()
    return re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:120]


def _norm(value):
    """xlsx and sqlite disagree about blanks. One representation: None."""
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
        self.xlsx_path = self.base_dir / "artifacts/jobs/org-roles-tracker.xlsx"
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
        """The tracker as the pipeline expects it: xlsx headers, xlsx order."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM roles ORDER BY row_order, rowid").fetchall()
        records = []
        for row in rows:
            record = {header: row[field] for field, header in DB_TO_XLSX.items()}
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
        if field not in DB_TO_XLSX:
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

    # ---------- xlsx bridge ----------

    def _xlsx_stamp(self):
        if not self.xlsx_path.exists():
            return None
        stat = self.xlsx_path.stat()
        return f"{int(stat.st_mtime)}:{stat.st_size}"

    def import_xlsx(self, actor="xlsx"):
        frame = pd.read_excel(self.xlsx_path)
        frame["_id"] = self._assign_ids(frame)
        summary = self.save_df(frame, actor=actor)
        self.set_meta("xlsx_stamp", self._xlsx_stamp())
        return summary

    def export_xlsx(self):
        frame = self.to_df().drop(columns=["_id"])
        self.xlsx_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_excel(self.xlsx_path, index=False)
        self.set_meta("xlsx_stamp", self._xlsx_stamp())
        self.set_meta("last_export", datetime.now().isoformat(timespec="seconds"))
        return self.xlsx_path

    def load(self, verbose=True):
        """Start-of-run entry point.

        Seeds the database from the spreadsheet the first time, and afterwards
        pulls in any hand edits made to the spreadsheet since the last export, so
        editing the xlsx by hand stays legal. Returns the tracker DataFrame.
        """
        if self.is_empty() and self.xlsx_path.exists():
            summary = self.import_xlsx(actor="migration")
            if verbose:
                print(f"  store: migrated {summary['inserted']} row(s) from the spreadsheet")
        elif self.xlsx_path.exists():
            stamp = self._xlsx_stamp()
            if stamp != self.get_meta("xlsx_stamp"):
                summary = self.import_xlsx(actor="xlsx-edit")
                if verbose and (summary["fields"] or summary["inserted"]):
                    print(f"  store: pulled {summary['fields']} hand edit(s) and "
                          f"{summary['inserted']} new row(s) from the spreadsheet")
        return self.to_df()

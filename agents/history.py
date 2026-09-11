"""
History and trends (daily, deterministic).

Every other agent overwrites its artifact, so "what changed this week?" had no answer.
This agent keeps the memory: it snapshots the artifacts that already exist into a small
SQLite database and diffs the newest snapshot against the one before it.

NO LLM CALLS. Nothing here is judged, generated, or inferred. It reads artifacts and
subtracts. If two runs are identical, the honest output is "nothing moved".

STORE: data/history.db
  role_snapshot(date, role_id, org, title, fit_score, recommendation, status, role_cat)
  radar_snapshot(date, headline, tier, theme)
  profile_snapshot(date, platform, coverage_pct)   # LinkedIn only; coverage is not
                                                   # meaningful for GitHub/BlueSky
  run_snapshot(date, counts_json)
All tables are keyed by date, so re-running the same day UPDATES the row instead of
appending a duplicate.

SOURCES (read only, never recomputed):
  artifacts/jobs/intel-*.json          roles + run counts
  artifacts/content/radar-*.json       picks
  artifacts/profiles/linkedin-scan-*.md  "### Keyword Coverage: NN%"

OUTPUT: artifacts/history/deltas-YYYY-MM-DD.json (plus get_deltas() for the renderer)

Run:  uv run python agents/history.py
      uv run python agents/history.py --show
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_RELPATH = "data/history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS role_snapshot (
    date           TEXT NOT NULL,
    role_id        TEXT NOT NULL,
    org            TEXT,
    title          TEXT,
    fit_score      INTEGER,
    recommendation TEXT,
    status         TEXT,
    role_cat       TEXT,
    PRIMARY KEY (date, role_id)
);
CREATE TABLE IF NOT EXISTS radar_snapshot (
    date     TEXT NOT NULL,
    headline TEXT NOT NULL,
    tier     TEXT,
    theme    TEXT,
    PRIMARY KEY (date, headline)
);
CREATE TABLE IF NOT EXISTS profile_snapshot (
    date         TEXT NOT NULL,
    platform     TEXT NOT NULL,
    coverage_pct REAL,
    PRIMARY KEY (date, platform)
);
CREATE TABLE IF NOT EXISTS run_snapshot (
    date       TEXT NOT NULL,
    counts_json TEXT,
    PRIMARY KEY (date)
);
CREATE INDEX IF NOT EXISTS idx_role_date ON role_snapshot(date);
CREATE INDEX IF NOT EXISTS idx_role_id   ON role_snapshot(role_id);
"""

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
COVERAGE_RE = re.compile(r"Keyword Coverage:\s*([0-9]+(?:\.[0-9]+)?)\s*%", re.I)


def _date_from_name(path):
    match = DATE_RE.search(Path(path).name)
    return match.group(1) if match else None


def _int_or_none(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


class History:
    """Snapshot store + delta engine for the daily artifacts."""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.db_path = self.base_dir / DB_RELPATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ read

    def _intel_files(self):
        return sorted((self.base_dir / "artifacts/jobs").glob("intel-*.json"))

    def _radar_files(self):
        return sorted((self.base_dir / "artifacts/content").glob("radar-*.json"))

    def _linkedin_files(self):
        return sorted((self.base_dir / "artifacts/profiles").glob("linkedin-scan-*.md"))

    @staticmethod
    def _read_json(path):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            return None

    # -------------------------------------------------------------- snapshot

    def snapshot_intel(self, path):
        """One intel-*.json -> role_snapshot rows + run_snapshot counts."""
        data = self._read_json(path)
        if not data:
            return 0
        date = data.get("date") or _date_from_name(path)
        if not date:
            return 0
        rows = []
        for role in data.get("roles") or []:
            rid = role.get("id")
            if not rid:
                continue
            rows.append((
                date, rid, role.get("org"), role.get("title"),
                _int_or_none(role.get("fit_score")),
                role.get("recommendation"), role.get("status"), role.get("role_cat"),
            ))
        with self.conn:
            # Replace the whole day, so a role that vanished from today's artifact
            # does not linger in today's snapshot.
            self.conn.execute("DELETE FROM role_snapshot WHERE date = ?", (date,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO role_snapshot "
                "(date, role_id, org, title, fit_score, recommendation, status, role_cat) "
                "VALUES (?,?,?,?,?,?,?,?)", rows)
            counts = data.get("counts") or {}
            if counts:
                self.conn.execute(
                    "INSERT OR REPLACE INTO run_snapshot (date, counts_json) VALUES (?,?)",
                    (date, json.dumps(counts, sort_keys=True)))
        return len(rows)

    def snapshot_radar(self, path):
        data = self._read_json(path)
        if not data:
            return 0
        date = data.get("date") or _date_from_name(path)
        if not date:
            return 0
        try:
            picks = data["sections"]["01_pillar_picks"]["picks"]
        except (KeyError, TypeError):
            picks = []
        rows = []
        for pick in picks or []:
            headline = (pick or {}).get("headline")
            if not headline:
                continue
            rows.append((date, headline, pick.get("tier"), pick.get("theme")))
        with self.conn:
            self.conn.execute("DELETE FROM radar_snapshot WHERE date = ?", (date,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO radar_snapshot (date, headline, tier, theme) "
                "VALUES (?,?,?,?)", rows)
        return len(rows)

    def snapshot_linkedin(self, path):
        """LinkedIn keyword coverage only. GitHub/BlueSky have no coverage metric."""
        date = _date_from_name(path)
        if not date:
            return 0
        try:
            text = Path(path).read_text()
        except OSError:
            return 0
        match = COVERAGE_RE.search(text)
        if not match:
            return 0
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO profile_snapshot (date, platform, coverage_pct) "
                "VALUES (?,?,?)", (date, "linkedin", float(match.group(1))))
        return 1

    def backfill(self):
        """Load every dated artifact on disk, so history starts real, not empty.

        Idempotent: each date is replaced, never appended twice.
        """
        stats = {"intel": 0, "radar": 0, "linkedin": 0}
        for path in self._intel_files():
            stats["intel"] += 1 if self.snapshot_intel(path) else 0
        for path in self._radar_files():
            stats["radar"] += 1 if self.snapshot_radar(path) else 0
        for path in self._linkedin_files():
            stats["linkedin"] += self.snapshot_linkedin(path)
        return stats

    # ----------------------------------------------------------------- dates

    def dates(self, table="role_snapshot"):
        rows = self.conn.execute(
            f"SELECT DISTINCT date FROM {table} ORDER BY date").fetchall()
        return [r["date"] for r in rows]

    def roles_on(self, date):
        rows = self.conn.execute(
            "SELECT * FROM role_snapshot WHERE date = ?", (date,)).fetchall()
        return {r["role_id"]: dict(r) for r in rows}

    # ---------------------------------------------------------------- deltas

    def deltas(self):
        """Diff the newest role snapshot against the one before it.

        Returns a dict that is deliberately allowed to be empty. `has_changes`
        is False when nothing moved, and the page says exactly that.
        """
        role_dates = self.dates("role_snapshot")
        out = {
            "date": role_dates[-1] if role_dates else None,
            "prev_date": role_dates[-2] if len(role_dates) > 1 else None,
            "days_between": None,
            "new_roles": [],
            "gone_roles": [],
            "fit_changes": [],
            "rec_changes": [],
            "status_changes": [],
            "coverage": None,
            "has_changes": False,
            "snapshots": len(role_dates),
        }
        if not out["prev_date"]:
            out["coverage"] = self._coverage_delta()
            out["has_changes"] = bool(out["coverage"] and out["coverage"].get("change"))
            return out

        try:
            out["days_between"] = (datetime.strptime(out["date"], "%Y-%m-%d")
                                   - datetime.strptime(out["prev_date"], "%Y-%m-%d")).days
        except ValueError:
            pass

        now = self.roles_on(out["date"])
        before = self.roles_on(out["prev_date"])

        def label(role):
            return {
                "role_id": role["role_id"],
                "org": role.get("org"),
                "title": role.get("title"),
                "fit_score": role.get("fit_score"),
                "recommendation": role.get("recommendation"),
            }

        for rid in sorted(set(now) - set(before), key=lambda k: (-(now[k]["fit_score"] or -1), k)):
            out["new_roles"].append(label(now[rid]))
        for rid in sorted(set(before) - set(now), key=lambda k: (-(before[k]["fit_score"] or -1), k)):
            out["gone_roles"].append(label(before[rid]))

        for rid in sorted(set(now) & set(before)):
            a, b = before[rid], now[rid]
            base = {"role_id": rid, "org": b.get("org"), "title": b.get("title")}
            if a.get("fit_score") != b.get("fit_score") and None not in (
                    a.get("fit_score"), b.get("fit_score")):
                out["fit_changes"].append({**base, "old": a["fit_score"], "new": b["fit_score"],
                                           "change": b["fit_score"] - a["fit_score"]})
            if (a.get("recommendation") or None) != (b.get("recommendation") or None):
                out["rec_changes"].append({**base, "old": a.get("recommendation"),
                                           "new": b.get("recommendation")})
            if (a.get("status") or None) != (b.get("status") or None):
                out["status_changes"].append({**base, "old": a.get("status"),
                                              "new": b.get("status")})

        out["fit_changes"].sort(key=lambda c: -abs(c["change"]))
        out["coverage"] = self._coverage_delta()

        out["has_changes"] = any([
            out["new_roles"], out["gone_roles"], out["fit_changes"],
            out["rec_changes"], out["status_changes"],
            bool(out["coverage"] and out["coverage"].get("change")),
        ])
        return out

    def _coverage_delta(self):
        rows = self.conn.execute(
            "SELECT date, coverage_pct FROM profile_snapshot "
            "WHERE platform = 'linkedin' AND coverage_pct IS NOT NULL "
            "ORDER BY date").fetchall()
        if not rows:
            return None
        latest = rows[-1]
        prev = rows[-2] if len(rows) > 1 else None
        return {
            "platform": "linkedin",
            "date": latest["date"],
            "pct": latest["coverage_pct"],
            "prev_date": prev["date"] if prev else None,
            "prev_pct": prev["coverage_pct"] if prev else None,
            "change": (round(latest["coverage_pct"] - prev["coverage_pct"], 1)
                       if prev else None),
        }

    # ------------------------------------------------------------------- run

    def run(self):
        print("Running History...")
        stats = self.backfill()
        print(f"  snapshotted intel={stats['intel']} radar={stats['radar']} "
              f"linkedin={stats['linkedin']} (dates: {len(self.dates())})")

        deltas = self.deltas()
        if not deltas["date"]:
            print("  no intel artifacts to snapshot yet")
        elif not deltas["prev_date"]:
            print(f"  only one snapshot ({deltas['date']}); no comparison possible yet")
        elif deltas["has_changes"]:
            print(f"  {deltas['prev_date']} -> {deltas['date']}: "
                  f"+{len(deltas['new_roles'])} new, -{len(deltas['gone_roles'])} gone, "
                  f"{len(deltas['fit_changes'])} fit, {len(deltas['rec_changes'])} rec, "
                  f"{len(deltas['status_changes'])} status")
        else:
            print(f"  {deltas['prev_date']} -> {deltas['date']}: nothing moved")

        out_dir = self.base_dir / "artifacts/history"
        out_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "agent": "history",
            "db": str(self.db_path.relative_to(self.base_dir)),
            "deltas": deltas,
        }
        out_path = out_dir / f"deltas-{today}.json"
        out_path.write_text(json.dumps(payload, indent=2))
        print(f"  wrote {out_path}")
        return out_path


def get_deltas(base):
    """Deltas for the renderer. Safe to call at any time; never raises."""
    try:
        hist = History(base)
    except sqlite3.Error:
        return None
    try:
        return hist.deltas()
    except sqlite3.Error:
        return None
    finally:
        hist.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic snapshot + delta history.")
    ap.add_argument("--show", action="store_true",
                    help="print the current deltas as JSON and exit without writing")
    args = ap.parse_args(argv)

    base = Path(__file__).resolve().parent.parent
    hist = History(base)
    try:
        if args.show:
            print(json.dumps(hist.deltas(), indent=2))
            return 0
        hist.run()
    finally:
        hist.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

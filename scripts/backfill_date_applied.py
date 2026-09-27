#!/usr/bin/env python3
"""One-off: fill Date Applied, and Stage, on roles already marked Applied.

The Applications tab times every nudge from Date Applied, but nothing ever set
it: roles moved to "03 Applied" with the date left blank. This fills it from
the best evidence the database has, in order:

  1. the first time the change log saw the status become "03 Applied"
  2. Last Updated
  3. Date Opened

never earlier than Date Opened. Only 1 is a real record; 2 and 3 are
estimates. Every date written here is logged with actor=backfill_date_applied,
and the Applications tab shows it as unconfirmed until you edit it.

Stage is filled from Outcomes where that names a stage ("Screen", "Final
round", ...), otherwise "Applied".

Only applied roles with an empty Date Applied (or Stage) are touched, so the
script is safe to run twice and never overwrites anything you set.

    uv run scripts/backfill_date_applied.py --dry-run
    uv run scripts/backfill_date_applied.py
    uv run scripts/backfill_date_applied.py --user ariel
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "agents"))

import workspace                                      # noqa: E402
from applications import BACKFILL_ACTOR as ACTOR      # noqa: E402
from job_intel import parse_outcome                   # noqa: E402
from store import STAGES, Store                       # noqa: E402

APPLIED = "03 Applied"

# Outcome label (job_intel.OUTCOME_LABELS) -> stage. Labels that end an
# application (Rejected, Ghosted, ...) say nothing about the stage reached.
STAGE_FROM_OUTCOME = {
    "Recruiter call": "Screen",
    "Screen": "Screen",
    "Interview": "Interviewing",
    "Onsite": "Final",
    "Final round": "Final",
    "Offer": "Offer",
}


def _day(value):
    text = str(value or "").strip()[:10]
    return text if len(text) == 10 and text[4] == "-" and text[7] == "-" else None


def plan(store):
    """[{id, org, title, date_applied?, date_source?, stage?}] for every applied
    role missing either field. Only the missing fields are in each entry."""
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, org, title, date_applied, date_opened, last_updated, stage, outcomes "
            "FROM roles WHERE status=? AND ("
            "  date_applied IS NULL OR TRIM(date_applied)='' OR stage IS NULL OR TRIM(stage)='') "
            "ORDER BY row_order", (APPLIED,))]
        applied_at = {r["role_id"]: r["ts"] for r in conn.execute(
            "SELECT role_id, MIN(ts) ts FROM changes WHERE field='status' AND new_value=? "
            "GROUP BY role_id", (APPLIED,))}

    planned = []
    for row in rows:
        entry = {"id": row["id"], "org": row["org"], "title": row["title"]}
        if not _day(row["date_applied"]):
            for source, value in (("change log", applied_at.get(row["id"])),
                                  ("last updated", row["last_updated"]),
                                  ("date opened", row["date_opened"])):
                day = _day(value)
                if day:
                    opened = _day(row["date_opened"])
                    if opened and day < opened:
                        day, source = opened, "date opened"
                    entry["date_applied"], entry["date_source"] = day, source
                    break
            else:
                entry["date_source"] = "no evidence"
        if not (row["stage"] or "").strip():
            label, _days, _display = parse_outcome(row["outcomes"])
            entry["stage"] = STAGE_FROM_OUTCOME.get(label, STAGES[0])
        # A role with no evidence for a date is listed even when there is
        # nothing to write, so the report keeps saying its date is missing.
        if len(entry) > 3:
            planned.append(entry)
    return planned


def apply(store, planned):
    written = 0
    for entry in planned:
        for field in ("date_applied", "stage"):
            if field in entry:
                # set_field logs to the change log: the Applications tab reads
                # actor=backfill_date_applied as "unconfirmed".
                written += store.set_field(entry["id"], field, entry[field], actor=ACTOR)["changed"]
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    workspace.add_argument(ap)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    args = ap.parse_args()

    ws = workspace.resolve(getattr(args, "user", None))
    store = Store(ws)
    print(f"workspace: {ws.name}")

    planned = plan(store)
    if not planned:
        print("every applied role has a date applied and a stage - nothing to do")
        return 0

    sources = {}
    for entry in planned:
        source = entry.get("date_source", "already set")
        sources[source] = sources.get(source, 0) + 1
        print(f"  {entry.get('date_applied', '-'):<10}  {source:<12}  "
              f"{entry.get('stage', '-'):<12}  {entry['org']} - {entry['title']}")
    print("  date applied: " + ", ".join(f"{n} {s}" for s, n in sorted(sources.items())))

    if args.dry_run:
        print("dry run - nothing written")
        return 0

    store.backup_db()
    print(f"updated {apply(store, planned)} field(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

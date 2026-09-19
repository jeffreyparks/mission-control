#!/usr/bin/env python3
"""One-off: fill the tracker's `source` field with where each role came from.

`source` used to hold "live" or "tracker" - which run found the row, not which
board it came from. Scanners now write "Anthropic (Greenhouse)", "JobSpy
(Indeed)", "BuiltIn" or "Manual" at fetch time. This rewrites the rows that
predate that, reading the board back out of the posting URL.

Only placeholder values are touched, so the script is safe to run twice and
never overwrites a real label or a value you typed yourself.

    uv run scripts/backfill_source.py --dry-run
    uv run scripts/backfill_source.py
    uv run scripts/backfill_source.py --user ariel
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "agents"))

import workspace                      # noqa: E402
import source_tags                    # noqa: E402
from store import Store               # noqa: E402

# Values written by the old scheme. Anything else is left alone: it is either
# already a source label, or something a human put there on purpose.
PLACEHOLDERS = {"", "live", "tracker", "manual", "manual-add", "auto-scan"}


def is_placeholder(value):
    text = (value or "").strip().lower()
    return text in PLACEHOLDERS or text.startswith("manual (")


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

    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, org, role_link, source FROM roles ORDER BY row_order")]

        planned, skipped = [], 0
        for row in rows:
            if not is_placeholder(row["source"]):
                skipped += 1
                continue
            label = source_tags.label_for(row["role_link"], row["org"])
            if label != (row["source"] or ""):
                planned.append((row["id"], row["source"], label))

        counts = {}
        for _id, _old, label in planned:
            counts[label] = counts.get(label, 0) + 1
        for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {count:>5}  {label}")
        print(f"  {len(rows)} row(s): {len(planned)} to set, "
              f"{skipped} already labelled, {len(rows) - len(planned) - skipped} unchanged")

        if args.dry_run:
            print("dry run - nothing written")
            return 0
        if not planned:
            print("nothing to do")
            return 0

        store.backup_db()
        now = datetime.now().isoformat(timespec="seconds")
        for role_id, old, label in planned:
            conn.execute("UPDATE roles SET source=?, updated_at=? WHERE id=?",
                         (label, now, role_id))
            # Same change log every other write uses, so the backfill is traceable.
            conn.execute(
                "INSERT INTO changes(role_id,field,old_value,new_value,actor,ts) "
                "VALUES(?,?,?,?,?,?)",
                (role_id, "source", old, label, "backfill_source", now))

    print(f"updated {len(planned)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

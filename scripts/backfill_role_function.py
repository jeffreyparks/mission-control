#!/usr/bin/env python3
"""One-off: tag every tracker role with its Function now, not at the next run.

The daily run tags new and changed roles as it goes (JobIntel.tag_functions).
This does the same for the whole existing tracker in one pass, so decisions
already made can be read against function straight away. It shares the run's
cache, so the next daily run reuses every tag made here instead of paying again.

Function is a pipeline label, like Sector: rows whose Function is already set
are left alone.

    uv run scripts/backfill_role_function.py --dry-run
    uv run scripts/backfill_role_function.py
    uv run scripts/backfill_role_function.py --user ariel
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "agents"))

import workspace                                  # noqa: E402
from job_intel import JobIntel, ROLE_FUNCTIONS    # noqa: E402

ACTOR = "backfill_role_function"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    workspace.add_argument(ap)
    ap.add_argument("--dry-run", action="store_true",
                    help="tag and report, write nothing to the tracker")
    args = ap.parse_args()

    ws = workspace.resolve(getattr(args, "user", None))
    print(f"workspace: {ws.name}")
    intel = JobIntel(ws)
    roles = [r for r in intel.tracker_roles(intel.load_tracker()) if not r.get("role_function")]
    if not roles:
        print("every role already has a function - nothing to do")
        return 0
    print(f"  {len(roles)} role(s) without a function")

    intel.tag_functions(roles)
    planned = [(r["store_id"], r["role_function"]) for r in roles if r.get("role_function")]

    counts = {}
    for _id, name in planned:
        counts[name] = counts.get(name, 0) + 1
    for name in ROLE_FUNCTIONS:
        if counts.get(name):
            print(f"  {counts[name]:>5}  {name}")
    print(f"  {len(planned)} to set, {len(roles) - len(planned)} left blank")
    print(f"  {intel.llm.report()}")

    if args.dry_run:
        print("dry run - nothing written to the tracker (tags are cached for the real run)")
        return 0
    if not planned:
        return 0

    intel.store.backup_db()
    for store_id, name in planned:
        intel.store.set_field(store_id, "role_function", name, actor=ACTOR)
    print(f"updated {len(planned)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

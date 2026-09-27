#!/usr/bin/env python3
"""One-off: fill Date Posted on existing tracker rows from the boards themselves.

The daily run dates new finds as they arrive, and dates tracker rows that
today's scan still lists. This does the rest of the existing tracker now, in two
passes, neither of which calls an LLM:

  1. The configured company boards (Greenhouse, Lever, Ashby) are listed once
     each, and rows are matched to postings by URL.
  2. Rows still undated whose link is a Greenhouse or Lever posting, or a
     Built In job page, are fetched one by one - the ATS's public API, or the
     page's JSON-LD datePosted - with a pause between requests.

Only a board's own date is written - nothing is estimated here. Rows whose
posting is gone, or whose board gives no date (LinkedIn pages carry none;
Indeed refuses direct fetches), stay blank; the tracker page shows those as "~Nd", days since the
tracker first saw them.

    uv run scripts/backfill_date_posted.py --dry-run
    uv run scripts/backfill_date_posted.py
"""
import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "agents"))

import workspace                                   # noqa: E402
from job_fetch import _greenhouse_ids, _lever_ids, canonical_url, fetch_posting   # noqa: E402
from job_scanner import JobScanner                 # noqa: E402
from posted import to_iso_date                     # noqa: E402
from store import Store                            # noqa: E402

ACTOR = "backfill_date_posted"
PAUSE_SECONDS = 0.5
# Built In is a website, not an API: slower, so a run never looks like a scrape.
BUILTIN_PAUSE_SECONDS = 1.5


def fetchable(url):
    """Pause to leave after fetching this link, or None when it is not worth
    fetching one by one."""
    clean = canonical_url(url)
    if _greenhouse_ids(clean) or _lever_ids(clean):
        return PAUSE_SECONDS
    if "builtin.com/job/" in clean:
        return BUILTIN_PAUSE_SECONDS
    return None


def board_dates(scanner):
    """{url: iso date} from every configured company board, one request each."""
    dates = {}
    for company in scanner.load_sources().get("companies", []):
        api_url, name = company.get("api_url") or "", company["name"]
        if "greenhouse" in api_url:
            jobs = scanner.fetch_greenhouse_jobs(name, api_url)
        elif "lever" in api_url:
            jobs = scanner.fetch_lever_jobs(name, api_url)
        elif "ashbyhq" in api_url:
            jobs = scanner.fetch_ashby_jobs(name, api_url)
        else:
            continue
        for job in jobs:
            day = to_iso_date(job.get("posted"))
            if day and job.get("url"):
                dates[job["url"]] = day
                dates[canonical_url(job["url"])] = day
    return dates


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    workspace.add_argument(ap)
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--no-fetch", action="store_true",
                    help="board listings only; skip fetching single postings")
    args = ap.parse_args()

    ws = workspace.resolve(getattr(args, "user", None))
    store = Store(ws)
    print(f"workspace: {ws.name}")
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, role_link FROM roles WHERE (date_posted IS NULL OR date_posted='') "
            "AND role_link IS NOT NULL AND role_link!=''")]
    print(f"  {len(rows)} undated row(s) with a link")

    dates = board_dates(JobScanner(ws))
    planned = {}
    for row in rows:
        day = dates.get(row["role_link"]) or dates.get(canonical_url(row["role_link"]))
        if day:
            planned[row["id"]] = day
    print(f"  pass 1 - company boards: {len(planned)} dated")

    if not args.no_fetch:
        todo = [r for r in rows if r["id"] not in planned and fetchable(r["role_link"])]
        print(f"  pass 2 - fetching {len(todo)} single posting(s) "
              f"(Greenhouse, Lever, Built In)...", flush=True)
        found = 0
        for num, row in enumerate(todo, 1):
            day = to_iso_date(fetch_posting(row["role_link"]).get("posted"))
            if day:
                planned[row["id"]] = day
                found += 1
            if num % 100 == 0:
                print(f"    {num}/{len(todo)} fetched, {found} dated", flush=True)
            time.sleep(fetchable(row["role_link"]))
        print(f"  pass 2: {found} dated ({len(todo) - found} gone or undated)")

    print(f"  {len(planned)} to set, {len(rows) - len(planned)} left blank")
    if args.dry_run:
        print("dry run - nothing written")
        return 0
    if not planned:
        return 0
    store.backup_db()
    for role_id, day in planned.items():
        store.set_field(role_id, "date_posted", day, actor=ACTOR)
    print(f"updated {len(planned)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

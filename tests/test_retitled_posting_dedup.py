"""A live posting whose URL is already tracked (under any title) must not
become a ghost duplicate: judged forever, never appendable, never editable.

Root cause found live: Anthropic's board showed one job (same URL) as both
"AAA, Commercial" (tracked) and later "Applied AI Architect, Commercial"
(live, permanently un-appended and therefore uneditable on the dashboard).
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp())   # never write cache into the repo or a real workspace
sys.path.insert(0, str(REPO / "agents"))

from job_intel import JobIntel

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

intel = JobIntel(SCRATCH)
intel.max_live_roles = 50

SAME_URL = "https://job-boards.greenhouse.io/anthropic/jobs/5192805008"
retitled_posting = {
    "company": "Anthropic", "title": "Applied AI Architect, Commercial",
    "url": SAME_URL, "location": "", "priority": 2,
    "description": "measurement science attribution marketing science incrementality",
}
genuinely_new_posting = {
    "company": "Anthropic", "title": "Senior Marketing Science Lead",
    "url": "https://job-boards.greenhouse.io/anthropic/jobs/9999999999", "location": "",
    "priority": 2, "description": "measurement science attribution marketing science",
}

# Without tracker_urls (the old behavior): both roles pass through.
kept_old = intel.prefilter_live([retitled_posting, genuinely_new_posting], tracker_ids=set())
check("old behavior (no url dedup) let the retitled ghost through - confirms the repro",
      any(r["url"] == SAME_URL for r in kept_old))

# With tracker_urls (the fix): the retitled posting is excluded; the genuinely
# new one still comes through untouched.
kept_new = intel.prefilter_live([retitled_posting, genuinely_new_posting],
                                 tracker_ids=set(), tracker_urls={SAME_URL})
check("a URL already tracked (under any title) is excluded from live results",
      not any(r["url"] == SAME_URL for r in kept_new))
check("a genuinely new URL still comes through", any(r["url"] == genuinely_new_posting["url"] for r in kept_new))
check("no LLM judgment is ever spent on the excluded ghost (it's gone before the caller sees it)",
      len(kept_new) == 1)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

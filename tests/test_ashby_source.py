"""Ashby board fetch: does it return usable job records? Live network call to a
public, unauthenticated API - no fixture needed, but skips gracefully if the
network is unavailable."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

from job_scanner import JobScanner

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

scanner = JobScanner(REPO)
jobs = scanner.fetch_ashby_jobs("OpenAI", "https://api.ashbyhq.com/posting-api/job-board/openai")

if not jobs:
    print("SKIP: no jobs returned (network unavailable or API changed) - not a hard failure")
    sys.exit(0)

check("returns a real number of postings", len(jobs) > 50, str(len(jobs)))
sample = jobs[0]
check("has a title", bool(sample.get("title")))
check("has a jobs.ashbyhq.com url", "ashbyhq.com" in (sample.get("url") or ""), sample.get("url"))
check("has a location", "location" in sample)
check("description includes the title", sample["title"] in sample.get("description", ""))
check("description is longer than just the title", len(sample.get("description", "")) > len(sample["title"]) + 20)
check("description has no leftover html tags", "<" not in sample.get("description", ""))
check("company is set", sample.get("company") == "OpenAI")

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

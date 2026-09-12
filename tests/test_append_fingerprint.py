"""Does appending a live find keep its fit fingerprint stable?

If it does not, every appended role is re-judged at full price on the next run.
No LLM calls here: fingerprints only.
"""
import json, shutil, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import pandas as pd
from job_intel import JobIntel, NEW_FIND_STATUS

work = Path(tempfile.mkdtemp())
(work / "artifacts/jobs").mkdir(parents=True)
(work / "data").mkdir(parents=True)
(work / "config").mkdir(parents=True)
shutil.copy2(REPO / "config/career-goals.md", work / "config/career-goals.md")

cols = ["Org","Title","Role Cat","Priority","Date Opened","Date Applied","Status","Outcomes",
        "Source","Match Score","Keywords Matched","Role Link","Range","Notes","Other Links",
        "Last Updated","Fit Score","Fit Rationale","Recommendation","Sector","Days To Outcome",
        "Role Cat (suggested)"]
seed = pd.DataFrame([{c: None for c in cols}])
seed.loc[0, ["Org","Title","Status"]] = ["Seed Co", "Seed Role", "01 Open"]
seed.to_excel(work / "artifacts/jobs/org-roles-tracker.xlsx", index=False)

intel = JobIntel(work)
intel.ctx = "CONTEXT"

live = {
    "id": "acme-head-of-measurement",
    "row_index": None,
    "source": "live",
    "org": "Acme",
    "title": "Head of Measurement",
    "url": "https://example.com/job/1",
    "location": "New York, NY",
    "comp_range": "220-280",
    "notes": None,
    "jd": "Own incrementality and MMM for the ads business.",
}
before = intel.fingerprint(live)

df = intel.load_tracker()
df, added = intel.append_new_finds(df, [live])
intel.save_tracker(df)

reloaded = intel.load_tracker()
roles = intel.tracker_roles(reloaded)
row = [r for r in roles if r["id"] == live["id"]]

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

check("row was appended", added == 1)
check("role is now a tracker row", len(row) == 1)
if row:
    after = intel.fingerprint(row[0])
    check("fingerprint survives the append", before == after, f"{before} vs {after}")
    check("status marks it as a new find", row[0]["status"] == NEW_FIND_STATUS)
    check("notes left for the user", not row[0].get("notes"))
    check("location recovered from the jd cache", row[0].get("location") == "New York, NY")
    check("jd recovered from the jd cache", bool(row[0].get("jd")))
    check("block is byte-identical",
          intel._role_block(live) == intel._role_block(row[0]))

print()
print("FAILURES:", fails if fails else "none")
shutil.rmtree(work, ignore_errors=True)
sys.exit(1 if fails else 0)

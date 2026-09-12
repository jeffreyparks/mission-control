"""Regression test: a role appended THIS run must get a usable store_id
immediately, not only starting with the next day's render.

Without this, the dashboard has nothing to address a brand-new "00 New find"
row by and silently renders it with no edit controls at all - exactly the
"Acceleration Partners" symptom this fixes. No LLM calls: fit/category/sector
logic is skipped, only the append -> update_tracker -> backfill sequence
that agents/job_intel.py's run() performs is exercised directly.
"""
import shutil, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import pandas as pd
from job_intel import JobIntel
from store import Store

work = Path(tempfile.mkdtemp())
(work / "artifacts/jobs").mkdir(parents=True)
(work / "data").mkdir(parents=True)
(work / "config").mkdir(parents=True)
shutil.copy2(REPO / "config/career-goals.md", work / "config/career-goals.md")

cols = list(__import__("store").COLUMN_MAP.keys())
seed = pd.DataFrame([{c: None for c in cols}])
seed.loc[0, ["Org", "Title", "Status"]] = ["Seed Co", "Seed Role", "01 Open"]
seed.to_excel(work / "artifacts/jobs/org-roles-tracker.xlsx", index=False)

intel = JobIntel(work)
intel.ctx = "CONTEXT"

live_role = {
    "id": "acceleration-partners-affiliate-manager",
    "row_index": None,
    "source": "live",
    "org": "Acceleration Partners",
    "title": "Affiliate Manager",
    "url": "https://example.com/job/9",
    "location": "Remote",
    "comp_range": None,
    "notes": None,
    "jd": "Manage affiliate partnerships and performance marketing programs.",
    "priority": None,
}

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

check("no store_id before the append", not live_role.get("store_id"))

# Mirror run()'s sequence: append, persist, then backfill store_id the way
# run() now does immediately after update_tracker.
df = intel.load_tracker()
df, added = intel.append_new_finds(df, [live_role])
intel.update_tracker(df, [live_role])

check("row was appended", added == 1)
check("store_id still missing right after append_new_finds/update_tracker alone",
      not live_role.get("store_id"))

store_df = Store(work).to_df()
try:
    live_role["store_id"] = store_df.iloc[live_role["row_index"]]["_id"]
except IndexError:
    pass

check("store_id populated after the backfill step", bool(live_role.get("store_id")),
      str(live_role.get("store_id")))

if live_role.get("store_id"):
    match = store_df[store_df["_id"] == live_role["store_id"]]
    check("the backfilled store_id resolves to the right row in the store",
          len(match) == 1 and match.iloc[0]["Org"] == "Acceleration Partners",
          str(match[["Org", "Title"]].to_dict("records") if len(match) else "no match"))

print()
print("FAILURES:", fails if fails else "none")
shutil.rmtree(work, ignore_errors=True)
sys.exit(1 if fails else 0)

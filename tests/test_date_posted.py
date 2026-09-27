"""Date Posted: every source's date shape normalises to one ISO date, tracker
rows pick up the board date from the day's scan by URL, and the page shows a
board date plainly and a first-seen stand-in as an estimate."""
import sys, tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "render"))

import pandas as pd
import build
from job_intel import JobIntel
from posted import days_since, to_iso_date
from store import COLUMN_MAP, Store

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

# ---- normalisation --------------------------------------------------------
check("greenhouse offset timestamp", to_iso_date("2024-12-20T13:53:38-05:00") == "2024-12-20")
check("ashby Z timestamp", to_iso_date("2026-03-12T16:38:15.322Z") == "2026-03-12")
check("lever epoch ms", to_iso_date(1758000000000) == "2025-09-16")
check("epoch ms as a string", to_iso_date("1758000000000") == "2025-09-16")
check("plain date", to_iso_date("2026-09-20") == "2026-09-20")
check("datetime (Built In badge)", to_iso_date(datetime(2026, 9, 25, 14, 0)) == "2026-09-25")
check("blank and junk are None",
      all(to_iso_date(v) is None for v in (None, "", "nan", "NaT", "soon", 0)))
check("a future date is refused", to_iso_date((date.today() + timedelta(days=3)).isoformat()) is None)
check("days_since", days_since((date.today() - timedelta(days=4)).isoformat()) == 4
      and days_since(None) is None)

# ---- tracker rows dated from the day's scan -------------------------------
work = Path(tempfile.mkdtemp())
for sub in ("config", "artifacts/jobs", "data", "me"):
    (work / sub).mkdir(parents=True)
cols = list(COLUMN_MAP.keys())
store = Store(work)
with store.connect() as conn:
    names = {r["name"] for r in conn.execute("PRAGMA table_info(roles)")}
check("store has date_posted", "date_posted" in names)

rows = [
    {**{c: None for c in cols}, "Org": "Acme", "Title": "Head of Data",
     "Role Link": "https://boards.greenhouse.io/acme/jobs/1", "Status": "00 New find"},
    {**{c: None for c in cols}, "Org": "Beta", "Title": "VP Analytics",
     "Role Link": "https://jobs.lever.co/beta/2", "Status": "01 Open"},
]
store.save_df(pd.DataFrame(rows, columns=cols), actor="test-seed")
intel = JobIntel(work)
df = intel.load_tracker()
tracked = intel.tracker_roles(df)
raw = [{"url": "https://boards.greenhouse.io/acme/jobs/1", "posted": "2026-09-20T09:00:00-04:00"},
       {"url": "https://elsewhere.example/9", "posted": "2026-09-21"}]
check("scan dates the matching tracker row", JobIntel.fill_posted_dates(tracked, raw) == 1)
check("board date lands on the role", tracked[0]["date_posted"] == "2026-09-20")
check("an unmatched row stays undated", tracked[1]["date_posted"] is None)

intel.update_tracker(df, tracked)
stored = Store(work).to_df().set_index("Org")
check("Date Posted written to the tracker", stored.loc["Acme", "Date Posted"] == "2026-09-20")
check("no date is invented for the other row", pd.isna(stored.loc["Beta", "Date Posted"]))

# ---- live finds keep the board date when appended -------------------------
live = {"id": "gamma--analyst", "org": "Gamma", "title": "Analyst", "url": "https://x/3",
        "row_index": None, "date_posted": "2026-09-24", "status": "01 Open"}
df2, added = intel.append_new_finds(intel.load_tracker(), [live])
check("new find appended with its posting date",
      added == 1 and df2.iloc[-1]["Date Posted"] == "2026-09-24")

# ---- the page -------------------------------------------------------------
today = date.today()
ago = lambda n: (today - timedelta(days=n)).isoformat()
roles = [
    {"org": "A", "title": "Fresh", "date_posted": ago(1), "date_opened": ago(1), "store_id": "a"},
    {"org": "B", "title": "Seen", "date_posted": None, "date_opened": ago(12), "store_id": "b"},
    {"org": "C", "title": "Unknown", "date_posted": None, "date_opened": None, "store_id": "c"},
]
ctx = build._tracker_context({"roles": roles, "orgs": []})
check("board date gives an age", roles[0]["posted_days"] == 1 and roles[0]["posted_basis"] == "board")
check("first seen stands in, flagged", roles[1]["posted_days"] == 12
      and roles[1]["posted_basis"] == "first seen")
check("no date at all is None", roles[2]["posted_days"] is None)

html = build._env().get_template("tracker.html.j2").render(**ctx)
check("posted filter rendered", 'id="postedf"' in html and 'data-posted="7"' in html)
check("fresh board date highlighted", 'class="posted fresh"' in html and ">1d<" in html)
check("estimate marked with ~, never highlighted", 'class="posted est"' in html and ">~12d<" in html)
check("board ages drive the filter", 'data-posted-days="1"' in html)
check("estimates are left out of the filter", 'data-posted-days="12"' not in html)

if fails:
    print(f"\n{len(fails)} failure(s): {fails}")
    sys.exit(1)
print("\nall date-posted checks passed")

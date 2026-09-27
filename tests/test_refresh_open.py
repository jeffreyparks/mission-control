"""--refresh-open: roles still in play go back to the model, closed roles keep
their stored verdicts, and the category/function caches stay in use."""
import sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import pandas as pd
from job_intel import FIT_FIELDS, JobIntel
from store import COLUMN_MAP, Store

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

work = Path(tempfile.mkdtemp())
for sub in ("config", "artifacts/jobs", "data", "me"):
    (work / sub).mkdir(parents=True)
cols = list(COLUMN_MAP.keys())
rows = [{**{c: None for c in cols}, "Org": org, "Title": "Head of Data", "Status": status}
        for org, status in (("Open", "00 New find"), ("Researching", "02 Researching"),
                            ("Applied", "03 Applied"), ("Closed", "04 Closed"))]
Store(work).save_df(pd.DataFrame(rows, columns=cols), actor="test-seed")


def stored_verdicts(intel, roles):
    """Every role already has a verdict stored under its current fingerprint."""
    by_fp = {}
    for role in roles:
        by_fp[intel.fingerprint(role)] = {**{f: None for f in FIT_FIELDS}, "fit_score": 40,
                                          "recommendation": "skip"}
    return by_fp, {}


for mode, expect in (({}, set()),
                     ({"refresh_open": True}, {"Open", "Researching", "Applied"}),
                     ({"refresh": True}, {"Open", "Researching", "Applied", "Closed"})):
    intel = JobIntel(work, **mode)
    roles = intel.tracker_roles(intel.load_tracker())
    intel.load_prior_fits = lambda roles=roles, intel=intel: stored_verdicts(intel, roles)
    fresh, reused = intel.split_by_freshness(roles)
    label = next(iter(mode), "default")
    check(f"{label}: re-judges {sorted(expect) or 'nothing'}",
          {r["org"] for r in fresh} == expect, str(sorted(r["org"] for r in fresh)))
    check(f"{label}: reuses the rest", reused == 4 - len(expect), str(reused))

check("refresh-open leaves the category/function caches in use",
      JobIntel(work, refresh_open=True).refresh is False)

if fails:
    print(f"\n{len(fails)} failure(s): {fails}")
    sys.exit(1)
print("\nall refresh-open checks passed")

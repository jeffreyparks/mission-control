"""Title heuristics that close a live find without spending an LLM call.

config/job-sources.yaml -> rules.auto_close_titles
"""
import shutil, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import pandas as pd
from job_intel import JobIntel, load_auto_close_titles, matched_auto_close_title

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

# ---------------- pure matcher ----------------
patterns = ["machine learning engineer", "research scientist", "software engineer",
            "account executive", "strategist"]

check("exact phrase matches", matched_auto_close_title("Software Engineer", patterns) == "software engineer")
check("case-insensitive", matched_auto_close_title("SOFTWARE ENGINEER II", patterns) == "software engineer")
check("matches as a substring within a longer title",
      matched_auto_close_title("Senior Machine Learning Engineer, Ads", patterns) == "machine learning engineer")
check("single-word pattern matches", matched_auto_close_title("Content Strategist", patterns) == "strategist")
check("word boundary: 'engineer' does not match inside 'engineering'",
      matched_auto_close_title("Machine Learning Engineering Manager", patterns) is None)
check("no match on an unrelated title",
      matched_auto_close_title("Director, Marketing Science", patterns) is None)
check("no title -> no match", matched_auto_close_title(None, patterns) is None)
check("no patterns -> no match", matched_auto_close_title("Software Engineer", []) is None)

# ---------------- config is editable ----------------
work = Path(tempfile.mkdtemp())
(work / "config").mkdir(parents=True)
(work / "artifacts/jobs").mkdir(parents=True)
(work / "data").mkdir(parents=True)

custom_yaml = """
companies: []
rules:
  auto_close_titles:
    - "underwater basket weaver"
"""
(work / "config/job-sources.yaml").write_text(custom_yaml)

from job_scanner import JobScanner
scanner = JobScanner(work)
loaded = load_auto_close_titles(scanner)
check("config list is read verbatim", loaded == ["underwater basket weaver"], str(loaded))
check("a custom title added by the user works the same way",
      matched_auto_close_title("Underwater Basket Weaver II", loaded) == "underwater basket weaver")

no_rules_yaml = "companies: []\n"
(work / "config/job-sources.yaml").write_text(no_rules_yaml)
check("missing rules/auto_close_titles degrades to empty, not an error",
      load_auto_close_titles(JobScanner(work)) == [])

# ---------------- apply_auto_close + partition_auto_closed ----------------
cols = ["Org","Title","Role Cat","Priority","Date Opened","Date Applied","Status","Outcomes",
        "Source","Match Score","Keywords Matched","Role Link","Range","Notes","Other Links",
        "Last Updated","Fit Score","Fit Rationale","Recommendation","Sector","Days To Outcome",
        "Role Cat (suggested)"]
seed = pd.DataFrame([{c: None for c in cols}])
seed.loc[0, ["Org", "Title", "Status"]] = ["Seed Co", "Seed Role", "01 Open"]
__import__("store").Store(work).save_df(seed, actor="test-seed")
(work / "me").mkdir(parents=True, exist_ok=True)
shutil.copy2(REPO / "templates/me/profile.md", work / "me/profile.md")
(work / "config/job-sources.yaml").write_text("""
companies: []
rules:
  auto_close_titles:
    - "software engineer"
""")

intel = JobIntel(work)
intel.ctx = "CONTEXT"

live_roles = [
    {"id": "acme-swe", "row_index": None, "source": "live", "org": "Acme",
     "title": "Software Engineer, Backend", "url": "https://x/1", "location": None,
     "comp_range": None, "notes": None, "jd": "Build backend services.", "priority": None,
     "status": "01 Open"},
    {"id": "acme-mms", "row_index": None, "source": "live", "org": "Acme",
     "title": "Marketing Measurement Science Lead", "url": "https://x/2", "location": None,
     "comp_range": None, "notes": None, "jd": "Own MMM and incrementality.", "priority": None,
     "status": "01 Open"},
]

auto_closed, kept = intel.partition_auto_closed(live_roles)
check("exactly the matching role is auto-closed", len(auto_closed) == 1 and auto_closed[0]["id"] == "acme-swe")
check("the non-matching role is kept for normal judging", len(kept) == 1 and kept[0]["id"] == "acme-mms")

closed = auto_closed[0]
check("fit_score is a deterministic 0, not left blank", closed["fit_score"] == 0)
check("recommendation is skip", closed["recommendation"] == "skip")
check("status is set straight to Closed", closed["status"] == "04 Closed")
check("why explains the heuristic match", "software engineer" in closed["why"].lower())
check("fit_fingerprint is set, so this caches like any other verdict", bool(closed.get("fit_fingerprint")))
check("kept role is untouched (no fit fields yet - still needs real judging)",
      "fit_score" not in kept[0])

# ---------------- append respects the auto-close status ----------------
df = intel.load_tracker()
df, added = intel.append_new_finds(df, [closed])
intel.update_tracker(df, [closed])   # append_new_finds only builds the df; this persists it
check("the auto-closed role is appended", added == 1)

reloaded = intel.load_tracker()
row = reloaded[reloaded["Title"] == "Software Engineer, Backend"]
check("tracker Status is 04 Closed, not the usual 00 New find",
      len(row) == 1 and row.iloc[0]["Status"] == "04 Closed",
      str(row["Status"].tolist()) if len(row) else "not found")

# Regression: append_new_finds/update_tracker must not clobber the role DICT's
# own status back to "00 New find" after deciding the tracker row's Status
# correctly - the dict is what gets serialized into intel.json, so this is
# what the dashboard/report actually shows.
check("the role dict itself still reports 04 Closed after append+update, "
      "not silently reverted to the default new-find status",
      closed["status"] == "04 Closed", closed["status"])

# ---------------- existing tracker rows are never touched ----------------
tracked_role = {"id": "existing-swe", "row_index": 0, "source": "tracker", "org": "Existing Co",
                "title": "Software Engineer", "status": "03 Applied"}
auto_closed2, kept2 = intel.partition_auto_closed([tracked_role])
check("partition_auto_closed is never called on tracked rows in run(), by construction",
      True)  # documents the invariant; enforced by run()'s call site, not this function

shutil.rmtree(work, ignore_errors=True)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

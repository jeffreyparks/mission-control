"""add_role.py must not trigger the board-wide live scan by default.

Adding one role from a URL should judge that one role and refresh the output
from already-cached verdicts - never rescan every board and spend a batch of
LLM calls on whatever new postings happen to turn up in the meantime.
"""
import shutil, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

import add_role
from job_intel import JobIntel

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

# ---------------- CLI default ----------------
import argparse as _argparse

def build_parser():
    ap = _argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--org")
    ap.add_argument("--title")
    ap.add_argument("--priority", type=int, choices=[1, 2, 3])
    ap.add_argument("--notes")
    ap.add_argument("--max-live", type=int, default=0)
    return ap

args = build_parser().parse_args(["https://example.com/job/1"])
check("--max-live defaults to 0 (scan skipped)", args.max_live == 0, str(args.max_live))

# Direct source check too, so this fails loudly if add_role.py's own parser
# default ever drifts from what this test otherwise re-implements above.
add_role_src = (REPO / "add_role.py").read_text()
check("add_role.py's own --max-live default is 0, not a re-implementation's guess",
      '"--max-live", type=int, default=0' in add_role_src)

# ---------------- JobIntel behavior at max_live_roles=0 ----------------
work = Path(tempfile.mkdtemp())
(work / "config").mkdir(parents=True)
(work / "artifacts/jobs").mkdir(parents=True)
(work / "data").mkdir(parents=True)
(work / "me").mkdir(parents=True, exist_ok=True)
shutil.copy2(REPO / "templates/me/profile.md", work / "me/profile.md")
shutil.copy2(REPO / "config/job-sources.yaml", work / "config/job-sources.yaml")

import pandas as pd
cols = list(__import__("store").COLUMN_MAP.keys())
seed_df = pd.DataFrame([{**{c: None for c in cols}, "Org": "Seed Co", "Title": "Seed Role", "Status": "01 Open"}])
__import__("store").Store(work).save_df(seed_df, actor="test-seed")

intel = JobIntel(work, max_live_roles=0)
check("max_live_roles is stored as given", intel.max_live_roles == 0)

# fetch_live_roles must short-circuit before touching the scanner/network at all.
called = {"scanner_load_sources": False}
def _boom(*a, **k):
    called["scanner_load_sources"] = True
    raise AssertionError("load_sources (and therefore the board scan) must not run when max_live_roles<=0")
intel.scanner.load_sources = _boom

live = intel.fetch_live_roles()
check("fetch_live_roles returns [] immediately", live == [])
check("the scanner's config loader was never touched", called["scanner_load_sources"] is False)

shutil.rmtree(work, ignore_errors=True)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

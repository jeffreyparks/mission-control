"""The single keyword source: me/profile.md, and nothing else.

Replaces tests/test_auto_close_titles.py. rules.auto_close_titles was a second
title-exclude list and a per-aggregator `queries:` was a third keyword list;
both were removed in favour of the two lists in me/profile.md.

Guarantees:
  ## Target Keywords   raise the score on a match in the TITLE or DESCRIPTION
  ## Exclude Keywords  drop the posting on a match in the TITLE ONLY
  no keyword list anywhere in config/job-sources.yaml
"""
import sys, tempfile, shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import yaml
from job_scanner import JobScanner
from profile_keywords import load_keywords

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

work = Path(tempfile.mkdtemp())
(work / "me").mkdir(parents=True)
(work / "config").mkdir(parents=True)
(work / "me/profile.md").write_text("""
## Target Keywords
- marketing science
- incrementality

## Exclude Keywords
- intern
- engineer
""")

scanner = JobScanner(work)
rules = {"min_match_score": 0}
lists = load_keywords(work)
target, exclude = lists["target"], lists["exclude"]

def score(title, desc):
    return scanner.calculate_match_score(desc, target, exclude, rules, title)

# ---------------- Target Keywords: title OR description ----------------
s, m = score("Head of Marketing Science", "A vague description.")
check("a TITLE-only keyword match scores", s > 0 and "marketing science" in m, f"{s} {m}")

s, m = score("Director, Growth", "You will own incrementality work.")
check("a DESCRIPTION-only keyword match scores", s > 0 and "incrementality" in m, f"{s} {m}")

s, m = score("Director, Growth", "Nothing relevant here.")
check("no keyword anywhere scores 0", s == 0 and m == [], f"{s} {m}")

# ---------------- Exclude Keywords: TITLE ONLY ----------------
s, _ = score("Software Engineer", "All about marketing science and incrementality.")
check("an excluded TITLE is dropped even with strong keyword matches", s == 0, str(s))

s, m = score("Head of Measurement", "You will mentor interns and work with engineers. marketing science.")
check("an excluded word in the DESCRIPTION does NOT drop the role",
      s > 0 and "marketing science" in m, f"{s} {m}")

s, m = score("", "We hire interns, engineer things, and do marketing science.")
check("an empty title does NOT fall back to matching the description",
      s > 0, f"{s} {m}")

s, _ = score("Data Science Intern", "marketing science")
check("a plural/suffix title still excludes", s == 0, str(s))

s, m = score("Head of Internal Marketing Science", "x")
check("'intern' does not match inside 'Internal'", s > 0, f"{s} {m}")

# ---------------- no keyword list in the YAML ----------------
for name in ["config/job-sources.yaml"]:
    doc = yaml.safe_load((REPO / name).read_text()) or {}
    rules_block = doc.get("rules") or {}
    check(f"{name}: rules carries no keyword list",
          not ({"must_have_keywords", "exclude_keywords", "auto_close_titles"} & set(rules_block)),
          str(sorted(rules_block)))
    check(f"{name}: no aggregator carries a queries list",
          all("queries" not in (agg or {}) for agg in (doc.get("aggregators") or [])),
          str([sorted(a) for a in (doc.get("aggregators") or [])]))

# ---------------- Built In derives its searches from profile.md ----------------
import builtin_source
captured = {}
def fake_fetch(queries=None, categories=None, host=None, scope=None,
               max_pages=3, timeout=15, label="Built In"):
    captured["queries"] = queries
    return []

import job_scanner as js
original = js._fetch_builtin_jobs
js._fetch_builtin_jobs = fake_fetch
try:
    scanner.fetch_builtin_jobs("Built In", {"host": "builtin.com", "max_pages": 2})
finally:
    js._fetch_builtin_jobs = original

check("Built In queries come from profile.md Target Keywords",
      captured.get("queries") == target, str(captured.get("queries")))

js._fetch_builtin_jobs = fake_fetch
try:
    scanner.fetch_builtin_jobs("Built In", {"host": "builtin.com", "max_queries": 1})
finally:
    js._fetch_builtin_jobs = original
check("max_queries caps the Built In fan-out",
      captured.get("queries") == target[:1], str(captured.get("queries")))

# ---------------- parked keywords inside HTML comments stay parked ----------------
# profile.md documents commenting a block of bullets out as the way to park a
# keyword. A multi-line `<!-- ... -->` block must contribute NO keywords; the
# old line-by-line check only skipped the first line, so every parked bullet
# after it leaked into the board queries.
commented = Path(tempfile.mkdtemp())
(commented / "me").mkdir(parents=True)
(commented / "me/profile.md").write_text("""
## Target Keywords
- marketing science
- incrementality

<!-- - media mix modeling
- geo experiments
- self serve -->

## Exclude Keywords
- intern
<!-- - manager
- strategist -->
""")
parked = load_keywords(commented)
check("a multi-line comment adds no target keywords",
      parked["target"] == ["marketing science", "incrementality"], str(parked["target"]))
check("a multi-line comment adds no exclude keywords",
      parked["exclude"] == ["intern"], str(parked["exclude"]))
shutil.rmtree(commented, ignore_errors=True)

# ---------------- the removed machinery is really gone ----------------
import job_intel
for gone in ["load_auto_close_titles", "matched_auto_close_title"]:
    check(f"job_intel.{gone} is removed", not hasattr(job_intel, gone))
for gone in ["apply_auto_close", "partition_auto_closed"]:
    check(f"JobIntel.{gone} is removed", not hasattr(job_intel.JobIntel, gone))

shutil.rmtree(work, ignore_errors=True)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

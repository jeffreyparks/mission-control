"""Tests for the JobSpy board aggregator (agents/jobspy_source.py) and the
single keyword list it reads (agents/profile_keywords.py)."""
import sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import jobspy_source as js
import profile_keywords as pk

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

# ---------------- one keyword list, read from me/profile.md ----------------
PROFILE = """# Career Goals

## Target Keywords
*(blurb)*

- marketing science
- causal-inference
- <!-- e.g. placeholder -->

## Exclude Keywords

- junior
- entry level

## Notes

- not a keyword
"""
ws = Path(tempfile.mkdtemp())
(ws / "me").mkdir()
(ws / "me/profile.md").write_text(PROFILE)
lists = pk.load_keywords(ws)
check("target keywords are read", lists["target"] == ["marketing science", "causal-inference"], str(lists["target"]))
check("exclude keywords are read", lists["exclude"] == ["junior", "entry level"], str(lists["exclude"]))
check("a later section does not leak in", "not a keyword" not in lists["target"])
check("template placeholders are skipped", not any("<!--" in k for k in lists["target"]))
check("a missing profile is empty, not an error", pk.load_keywords(Path(tempfile.mkdtemp())) == {"target": [], "exclude": []})

# ---------------- query building ----------------
check("a multi-word term is quoted for an exact match",
      pk.build_board_query("marketing science") == '"marketing science"')
check("a single word is not quoted", pk.build_board_query("analytics") == "analytics")
check("slug spellings become phrases", pk.build_board_query("causal-inference") == '"causal inference"')
check("excludes become -terms",
      pk.build_board_query("analytics", ["junior", "entry level"]) == 'analytics -junior -"entry level"')
check("an empty term yields no query", pk.build_board_query("  ") == "")
check("the exclude list is capped",
      pk.build_board_query("x", [f"w{i}" for i in range(20)], max_excludes=3) == "x -w0 -w1 -w2")

# ---------------- site resolution ----------------
check("the shipped default is indeed + linkedin", js._resolve_sites({}, "t") == ["indeed", "linkedin"])
check("google is refused - it needs hand-copied syntax", js._resolve_sites({"sites": ["google"]}, "t") == [])
check("an unknown site is dropped", js._resolve_sites({"sites": ["indeed", "monster"]}, "t") == ["indeed"])
check("duplicates collapse", js._resolve_sites({"sites": ["indeed", "indeed"]}, "t") == ["indeed"])
check("a bare string works", js._resolve_sites({"sites": "indeed"}, "t") == ["indeed"])

# ---------------- queries from keywords, and the escape hatch ----------------
qs = js._resolve_queries({}, ["analytics", "marketing science"], ["junior"], "t")
check("one query per target keyword", qs == ["analytics -junior", '"marketing science" -junior'], str(qs))
check("max_queries caps the fan-out",
      len(js._resolve_queries({"max_queries": 2}, list("abcde"), [], "t")) == 2)
check("search_query overrides the derived queries",
      js._resolve_queries({"search_query": 'raw OR query'}, ["analytics"], ["junior"], "t") == ["raw OR query"])
check("no keywords means no queries", js._resolve_queries({}, [], [], "t") == [])

# ---------------- board parameter conflicts ----------------
kw = js._scrape_kwargs({"hours_old": 72, "is_remote": True, "job_type": "fulltime"}, "indeed", "t", set())
check("indeed keeps only hours_old of the conflicting filters",
      kw["hours_old"] == 72 and "is_remote" not in kw and "job_type" not in kw, str(sorted(kw)))
kw = js._scrape_kwargs({"is_remote": True}, "indeed", "t", set())
check("is_remote survives when it is unambiguous", kw.get("is_remote") is True)
check("country_indeed defaults for indeed", kw.get("country_indeed") == "USA")
kw = js._scrape_kwargs({}, "linkedin", "t", set())
check("country_indeed is not sent to linkedin", "country_indeed" not in kw)
check("linkedin descriptions stay off without proxies", kw["linkedin_fetch_description"] is False)
check("results_wanted is capped",
      js._scrape_kwargs({"results_wanted": 99999}, "indeed", "t", set())["results_wanted"] == js.HARD_MAX_RESULTS)

# ---------------- row mapping ----------------
NAN = float("nan")
row = {"title": "Director, Data Science", "company": "Acme", "job_url": "https://indeed.com/viewjob?jk=1",
       "location": "Austin, TX, US", "description": "Own measurement.", "date_posted": "2026-09-16",
       "min_amount": 170000.0, "max_amount": 230000.0, "interval": "yearly", "site": "indeed",
       "job_url_direct": "https://acme.com/jobs/1"}
job = js._to_job(row, "JobSpy")
check("company/title/url map across", (job["company"], job["title"], job["url"]) ==
      ("Acme", "Director, Data Science", "https://indeed.com/viewjob?jk=1"))
check("description is prefixed with the title so title-only matches still score",
      job["description"] == "Director, Data Science Own measurement.")
check("posted is a plain ISO date", job["posted"] == "2026-09-16")
check("salary becomes a comp range", job["comp_range"] == "170K-230K", job["comp_range"])
check("the employer's own link is kept alongside the board link",
      job["direct_url"] == "https://acme.com/jobs/1")

check("hourly pay keeps its interval",
      js._to_job({**row, "min_amount": 55, "max_amount": 75, "interval": "hourly"}, "t")["comp_range"] == "55-75/hr")
check("a single-sided band still renders",
      js._to_job({**row, "min_amount": 170000, "max_amount": NAN}, "t")["comp_range"] == "170K")
check("missing salary is empty, not guessed",
      js._to_job({**row, "min_amount": NAN, "max_amount": NAN}, "t")["comp_range"] == "")
check("NaN date is empty, not invented", js._to_job({**row, "date_posted": NAN}, "t")["posted"] == "")
check("NaT date is empty too", js._to_job({**row, "date_posted": "NaT"}, "t")["posted"] == "")
check("a row with no title is dropped", js._to_job({**row, "title": NAN}, "t") is None)
check("a row with no url is dropped", js._to_job({**row, "job_url": ""}, "t") is None)
check("a missing company is empty, not 'nan'", js._to_job({**row, "company": NAN}, "t")["company"] == "")

# ---------------- failure handling ----------------
check("a 429 is recognised", js._is_rate_limited(Exception("HTTP 429 Too Many Requests")))
check("a plain error is not", not js._is_rate_limited(Exception("connection reset")))

class _Frame(list):
    """Stands in for the DataFrame scrape_jobs returns."""
    def to_dict(self, _orient):
        return list(self)

def run_with(fake, config=None):
    """Drive fetch_jobspy_jobs against a stubbed scrape_jobs, no network."""
    import jobspy
    original = jobspy.scrape_jobs
    jobspy.scrape_jobs = fake
    try:
        cfg = {"delay_seconds": 0, "sites": ["indeed"]}
        cfg.update(config or {})
        return js.fetch_jobspy_jobs(["analytics", "measurement"], ["junior"], cfg, "test")
    finally:
        jobspy.scrape_jobs = original

calls = []
def fake_ok(**kwargs):
    calls.append(kwargs)
    return _Frame([dict(row, job_url=f"https://indeed.com/viewjob?jk={len(calls)}")])

got = run_with(fake_ok)
check("every query is scraped", len(calls) == 2, str(len(calls)))
check("results come back mapped", len(got) == 2 and got[0]["company"] == "Acme")
check("the keyword-derived query reaches the board",
      calls[0]["search_term"] == "analytics -junior", calls[0]["search_term"])

def fake_dupes(**kwargs):
    return _Frame([row, row])
check("the same posting is only returned once", len(run_with(fake_dupes)) == 1)

def fake_429(**kwargs):
    calls.append(kwargs)
    raise Exception("Response 429 - too many requests")
calls.clear()
check("a rate-limited site stops instead of hammering", run_with(fake_429) == [] and len(calls) == 1, str(len(calls)))

partial = []
def fake_429_after_one(**kwargs):
    partial.append(kwargs)
    if len(partial) == 1:
        return _Frame([row])
    raise Exception("429 Too Many Requests")
kept = run_with(fake_429_after_one)
check("roles found before the block are kept", len(kept) == 1, str(len(kept)))

def fake_boom(**kwargs):
    calls.append(kwargs)
    raise RuntimeError("parse error")
calls.clear()
check("an ordinary failure skips that query and continues", run_with(fake_boom) == [] and len(calls) == 2)

check("no target keywords means no scan at all",
      js.fetch_jobspy_jobs([], [], {"sites": ["indeed"]}, "test") == [])

# ---------------- exclude matching: case, plurals, word boundaries ----------------
check("an exclude term is case-insensitive", pk.matches_exclude("Data Science Intern", ["intern"]) == "intern")
check("a capitalised term in the config still matches", pk.matches_exclude("summer intern", ["Intern"]) == "Intern")
check("a plural is caught", pk.matches_exclude("Summer Interns Program", ["intern"]) == "intern")
check("'internal' is NOT an intern role", pk.matches_exclude("Internal Analytics Lead", ["intern"]) is None)
check("'international' is NOT an intern role", pk.matches_exclude("International Product Manager", ["intern"]) is None)
check("spaces match hyphens", pk.matches_exclude("Entry-Level Analyst", ["entry level"]) == "entry level")
check("hyphens match spaces", pk.matches_exclude("entry level analyst", ["entry-level"]) == "entry-level")
check("the matching term is reported back", pk.matches_exclude("Junior Intern", ["senior", "intern"]) == "intern")
check("no excludes means nothing matches", pk.matches_exclude("Intern", []) is None)
check("empty text is safe", pk.matches_exclude("", ["intern"]) is None)
check("a term at the very start matches", pk.matches_exclude("Intern, Data", ["intern"]) == "intern")

def fake_intern(**kwargs):
    # run_with() passes ["junior"] as the exclude list.
    return _Frame([dict(row, title="Junior Data Scientist", job_url="https://indeed.com/viewjob?jk=i")])
check("an excluded title never leaves the source",
      run_with(fake_intern, {"sites": ["indeed"]}) == [])

def fake_internal(**kwargs):
    return _Frame([dict(row, title="Juniper Networks Analytics Lead", job_url="https://indeed.com/viewjob?jk=n")])
check("a lookalike title survives", len(run_with(fake_internal, {"sites": ["indeed"]})) == 1)

# ---------------- dependency overrides stay in sync ----------------
# python-jobspy's metadata pins NUMPY==1.26.3 and pandas<3 although it only
# calls pd.DataFrame, pd.concat and np.round. Two places carry the override:
# pyproject's [tool.uv] (for uv sync / uv run) and tool-overrides.txt (for
# `uv tool install`, which does not read that table). They must agree, or `mc`
# silently runs without jobspy installed.
import tomllib

pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
inline = set(pyproject.get("tool", {}).get("uv", {}).get("override-dependencies", []))
from_file = {line.strip() for line in (REPO / "tool-overrides.txt").read_text().splitlines()
             if line.strip() and not line.startswith("#")}
check("pyproject declares the jobspy overrides", inline, str(inline))
check("tool-overrides.txt matches pyproject", inline == from_file, f"{sorted(inline)} vs {sorted(from_file)}")
check("python-jobspy is an actual dependency",
      any(d.startswith("python-jobspy") for d in pyproject["project"]["dependencies"]))

print()
print("FAILURES (final):", fails if fails else "none")
sys.exit(1 if fails else 0)

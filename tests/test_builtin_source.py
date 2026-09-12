"""Tests for the Built In aggregator parser (agents/builtin_source.py)."""
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import builtin_source as bi

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

# ---------------- host resolution / SSRF guard ----------------
check("default host", bi.resolve_host(None) == "builtin.com")
check("default host, empty string", bi.resolve_host("") == "builtin.com")
check("bare market host resolves to www", bi.resolve_host("builtinseattle.com") == "www.builtinseattle.com")
check("www market host is idempotent", bi.resolve_host("www.builtinnyc.com") == "www.builtinnyc.com")
check("builtinchicago is .org, not .com", bi.resolve_host("builtinchicago.org") == "www.builtinchicago.org")
check("a pasted URL resolves by hostname", bi.resolve_host("https://www.builtinsf.com/jobs") == "www.builtinsf.com")
check("unknown host rejected", bi.resolve_host("evil.example.com") is None)
check("non-string host rejected", bi.resolve_host(123) is None)

try:
    bi.assert_host("http://builtin.com/jobs")
    check("assert_host rejects non-https", False)
except ValueError:
    check("assert_host rejects non-https", True)

try:
    bi.assert_host("https://evil.example.com/jobs")
    check("assert_host rejects an unlisted host", False)
except ValueError:
    check("assert_host rejects an unlisted host", True)

check("assert_host accepts an allowlisted host", bi.assert_host("https://builtin.com/jobs?page=1") == "https://builtin.com/jobs?page=1")

# ---------------- posted-badge parsing ----------------
now = datetime(2026, 6, 15, 12, 0, 0)
check("today", bi.parse_posted_at("Today", now) == now)
check("yesterday", bi.parse_posted_at("Yesterday", now) == now - __import__("datetime").timedelta(days=1))
check("N days ago", bi.parse_posted_at("3 Days Ago", now) == now - __import__("datetime").timedelta(days=3))
check("reposted N days ago", bi.parse_posted_at("Reposted 15 Days Ago", now) == now - __import__("datetime").timedelta(days=15))
check("an hour ago", bi.parse_posted_at("an hour ago", now) == now - __import__("datetime").timedelta(hours=1))
check("30+ days ago", bi.parse_posted_at("30+ Days Ago", now) == now - __import__("datetime").timedelta(days=30))
check("unrecognised text -> None", bi.parse_posted_at("Just now", now) is None)
check("non-string -> None", bi.parse_posted_at(None, now) is None)

# ---------------- salary parsing ----------------
check("annual band", bi.parse_salary("170K-230K Annually") == (170000, 230000))
check("single-sided annual band", bi.parse_salary("150K Annually") == (150000, 150000))
check("hourly band is deliberately NOT parsed", bi.parse_salary("115K-130K Hourly") is None)
check("garbage -> None", bi.parse_salary("competitive") is None)
check("non-string -> None", bi.parse_salary(None) is None)

# ---------------- location composition ----------------
check("mode + one place", bi.compose_location("Hybrid", ["Chicago, IL"]) == "Hybrid · Chicago, IL")
check("mode + many places", bi.compose_location("Remote or Hybrid", ["NY", "SF"]) == "Remote or Hybrid · NY · SF")
check("bare remote mode, no places, kept", bi.compose_location("Remote", []) == "Remote")
check("bare non-remote mode, no places, DROPPED (unresolved multi-location)",
      bi.compose_location("Hybrid", []) == "")
check("no mode, no places -> empty", bi.compose_location("", []) == "")
check("no mode, with places", bi.compose_location("", ["Austin, TX"]) == "Austin, TX")

# ---------------- card + spine parsing on a synthetic page ----------------
SAMPLE_HTML = """
<script type="application/ld+json">
{"@type":"ItemList","numberOfItems":2,"itemListElement":[
{"@type":"ListItem","position":1,"name":"Senior Data Scientist, Marketing Science","url":"https://builtin.com/job/senior-data-scientist/12345","description":"Own experimentation and MMM for a growing ads business. SQL, causal inference."},
{"@type":"ListItem","position":2,"name":"Marketing Analyst","url":"https://builtin.com/job/marketing-analyst/67890"}
]}
</script>
<div><a href="/company/acme-co" class="text-pretty-blue">Acme Co</a></div>
<div data-id="job-card" data-builtin-track-job-id="12345">
  <h2><a href="/job/senior-data-scientist/12345" data-id="job-card-title" data-builtin-track-job-id="12345">Senior Data Scientist, Marketing Science</a></h2>
  <span><i class="fa-clock"></i></span><span>3 Days Ago</span>
  <span><i class="fa-house-building"></i></span><span>Hybrid</span>
  <span><i class="fa-location-dot"></i></span><span>Chicago, IL, USA</span>
  <span><i class="fa-sack-dollar"></i></span><span>170K-230K Annually</span>
</div>
<div><a href="/company/widgetsinc" class="text-pretty-blue">Widgets Inc</a></div>
<div data-id="job-card" data-builtin-track-job-id="67890">
  <h2><a href="/job/marketing-analyst/67890" data-id="job-card-title" data-builtin-track-job-id="67890">Marketing Analyst</a></h2>
  <span><i class="fa-house-building"></i></span><span>Remote</span>
</div>
"""

parsed = bi.parse_list_page(SAMPLE_HTML)
check("both spine entries parsed", len(parsed) == 2, str(len(parsed)))
by_url = {j["url"]: j for j in parsed}
j1 = by_url.get("https://builtin.com/job/senior-data-scientist/12345")
check("spine title present", j1 and j1["title"] == "Senior Data Scientist, Marketing Science")
check("spine description present", j1 and "experimentation" in j1["description"])
check("card enrichment joined by job id: company", j1 and j1["company"] == "Acme Co")
check("card enrichment joined by job id: location", j1 and j1["location"] == "Hybrid · Chicago, IL, USA")
check("card enrichment joined by job id: salary", j1 and j1.get("salary") == (170000, 230000))
check("card enrichment joined by job id: posted_at", j1 and j1.get("posted_at") is not None)

j2 = by_url.get("https://builtin.com/job/marketing-analyst/67890")
check("second card: company from its OWN preceding anchor, not the first card's",
      j2 and j2["company"] == "Widgets Inc")
check("second card: bare remote mode with no location text still yields 'Remote'",
      j2 and j2["location"] == "Remote")
check("second card: no salary badge -> no salary key", j2 and "salary" not in j2)

# A malformed ItemList entry must not abort the whole page.
BROKEN_HTML = SAMPLE_HTML.replace('"name":"Marketing Analyst"', '"name":BROKEN_NOT_JSON')
parsed_broken = bi.parse_list_page(BROKEN_HTML)
check("a malformed spine entry is skipped, not fatal", len(parsed_broken) == 1, str(len(parsed_broken)))

# No cards at all: still returns spine-only rows (title/url/description).
NO_CARDS_HTML = SAMPLE_HTML.split("<div><a href=\"/company/acme-co\"")[0]
parsed_no_cards = bi.parse_list_page(NO_CARDS_HTML)
check("spine survives with zero cards", len(parsed_no_cards) == 2, str(len(parsed_no_cards)))
check("company/location empty without cards, not guessed",
      all(j["company"] == "" and j["location"] == "" for j in parsed_no_cards))

# ---------------- fetch_builtin_jobs: config handling, no network ----------------
check("no queries/categories -> nothing to scan", bi.fetch_builtin_jobs() == [])
check("unknown host -> skipped, not an exception", bi.fetch_builtin_jobs(queries=["x"], host="evil.example.com") == [])

# ---------------- live network smoke test ----------------
# A real, unauthenticated GET against the actual board. Not a hard failure if
# the network is unavailable - matches tests/test_ashby_source.py's stance.
live = bi.fetch_builtin_jobs(queries=["marketing science"], max_pages=1, label="live smoke test")
if not live:
    print("SKIP: no jobs returned (network unavailable or the board changed) - not a hard failure")
else:
    check("live: returns a real number of postings", len(live) > 5, str(len(live)))
    sample = live[0]
    check("live: has a title", bool(sample.get("title")))
    check("live: has a builtin.com job url", "builtin.com/job/" in (sample.get("url") or ""), sample.get("url"))
    check("live: has a company", bool(sample.get("company")), sample)
    check("live: description includes the title", sample["title"] in sample.get("description", ""))
    check("live: description has no leftover html tags", "<" not in sample.get("description", ""))

print()
print("FAILURES (final):", fails if fails else "none")
sys.exit(1 if fails else 0)

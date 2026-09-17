"""Unit tests for the live-scan judging quota split (job_intel.prefilter_live)."""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp())   # never write cache into the repo or a real workspace
sys.path.insert(0, str(REPO / "agents"))

from job_intel import JobIntel, AGGREGATOR_PRIORITY, LOWER_TIER_RESERVE_FRACTION

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)


def make_jobs(n, company, title_fmt, priority):
    return [{"company": company, "title": title_fmt.format(i), "url": f"https://x/{company}/{i}",
             "location": "", "description": "measurement attribution marketing science", "priority": priority}
            for i in range(n)]


def run(raw, max_live_roles=200):
    intel = JobIntel(SCRATCH)
    intel.max_live_roles = max_live_roles
    return intel.prefilter_live(raw, tracker_ids=set())


check("reserve fraction is 25% by default", LOWER_TIER_RESERVE_FRACTION == 0.25)
check("aggregators are priority 1", AGGREGATOR_PRIORITY == 1)

# --- plenty of both: exact quota split -------------------------------------
plenty_lower = make_jobs(300, "Acme", "Marketing Science Lead {}", priority=2)
plenty_agg = make_jobs(300, "BuiltInCo", "Measurement Analyst {}", priority=1)
roles = run(plenty_lower + plenty_agg, max_live_roles=200)
lower_count = sum(1 for r in roles if (r.get("priority") or 5) > AGGREGATOR_PRIORITY)
agg_count = sum(1 for r in roles if (r.get("priority") or 5) <= AGGREGATOR_PRIORITY)
check("total is exactly the cap when both pools are plentiful", len(roles) == 200, str(len(roles)))
check("lower tier gets its 25% reserve (50 of 200)", lower_count == 50, str(lower_count))
check("aggregator fills the rest (150 of 200)", agg_count == 150, str(agg_count))

# --- lower tier short: aggregator backfills --------------------------------
short_lower = make_jobs(10, "Acme", "Marketing Science Lead {}", priority=2)
roles = run(short_lower + plenty_agg, max_live_roles=200)
lower_count = sum(1 for r in roles if (r.get("priority") or 5) > AGGREGATOR_PRIORITY)
agg_count = sum(1 for r in roles if (r.get("priority") or 5) <= AGGREGATOR_PRIORITY)
check("cap still fully used when lower tier is short", len(roles) == 200, str(len(roles)))
check("all 10 available lower-tier roles are included", lower_count == 10, str(lower_count))
check("aggregator backfills the unused reserve (190 of 200)", agg_count == 190, str(agg_count))

# --- aggregator short: lower tier backfills ---------------------------------
short_agg = make_jobs(5, "BuiltInCo", "Measurement Analyst {}", priority=1)
roles = run(plenty_lower + short_agg, max_live_roles=200)
lower_count = sum(1 for r in roles if (r.get("priority") or 5) > AGGREGATOR_PRIORITY)
agg_count = sum(1 for r in roles if (r.get("priority") or 5) <= AGGREGATOR_PRIORITY)
check("cap still fully used when aggregator supply is short", len(roles) == 200, str(len(roles)))
check("all 5 available aggregator roles are included", agg_count == 5, str(agg_count))
check("lower tier backfills beyond its 25% reserve (195 of 200)", lower_count == 195, str(lower_count))

# --- both short: total is just whatever exists, no fabrication -------------
roles = run(short_lower + short_agg, max_live_roles=200)
check("both pools short: total is the true sum, not padded to the cap",
      len(roles) == 15, str(len(roles)))

# --- a single dominant source cannot take every slot when the other has supply
one_company_flood = make_jobs(500, "OpenAI", "Marketing Analyst {}", priority=2)
roles = run(one_company_flood + plenty_agg, max_live_roles=200)
agg_count = sum(1 for r in roles if (r.get("priority") or 5) <= AGGREGATOR_PRIORITY)
check("a flooded lower-tier source still leaves room for aggregator supply",
      agg_count == 150, str(agg_count))

# --- comp_range (Salary) survives from raw job dict into the role dict ------
salaried_job = {"company": "Acme", "title": "Marketing Science Lead", "url": "https://x/salary-1",
                "location": "", "description": "measurement attribution marketing science",
                "priority": 1, "comp_range": "$150,000 - $190,000"}
intel = JobIntel(SCRATCH)
intel.max_live_roles = 10
roles = intel.prefilter_live([salaried_job], tracker_ids=set())
check("comp_range threads through prefilter_live into the role dict",
      len(roles) == 1 and roles[0].get("comp_range") == "$150,000 - $190,000",
      str(roles[0].get("comp_range")) if roles else "no role kept")

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

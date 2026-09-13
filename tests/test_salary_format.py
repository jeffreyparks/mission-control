"""Salary formatting: concise "150k-190k" for annual figures, unchanged
dollar-and-unit format for anything that would misread if divided by 1000
(hourly, non-USD)."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import job_fetch as jf

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

check("round thousand, single value", jf.format_salary_k(150000) == "150k")
check("round thousand, range", jf.format_salary_k(130000, 185000) == "130k-185k")
check("non-round thousand keeps one decimal", jf.format_salary_k(162500, 187500) == "162.5k-187.5k")
check("equal low/high collapses to a single value", jf.format_salary_k(150000, 150000) == "150k")

# ---------------- schema.org MonetaryAmount, via _comp_from_base_salary ----------------
annual_range = {"value": {"minValue": 130000, "maxValue": 185000, "unitText": "YEAR"}, "currency": "USD"}
check("annual USD range -> concise k format", jf._comp_from_base_salary(annual_range) == "130k-185k")

no_unit = {"value": {"minValue": 150000, "maxValue": 200000}, "currency": "USD"}
check("no unit specified defaults to annual (concise)", jf._comp_from_base_salary(no_unit) == "150k-200k")

single_annual = {"value": {"value": 175000, "unitText": "yearly"}, "currency": "USD"}
check("single annual value -> concise k format", jf._comp_from_base_salary(single_annual) == "175k")

hourly = {"value": {"minValue": 45, "maxValue": 65, "unitText": "HOUR"}, "currency": "USD"}
result = jf._comp_from_base_salary(hourly)
check("hourly rate is NOT forced through k-notation (would misread as annual)",
      result is not None and "k" not in result.lower(), str(result))
check("hourly rate keeps its per-unit label", result and "hour" in result.lower(), str(result))

non_usd = {"value": {"minValue": 80000, "maxValue": 100000, "unitText": "YEAR"}, "currency": "EUR"}
result_eur = jf._comp_from_base_salary(non_usd)
check("non-USD currency is NOT forced through k-notation", result_eur and "k" not in result_eur.lower(), str(result_eur))
check("non-USD currency keeps its currency code", result_eur and "EUR" in result_eur, str(result_eur))

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

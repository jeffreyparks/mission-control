"""The fit-reuse fingerprint must ignore Notes and Salary (comp_range).

Both are dashboard-editable now; a personal note or a salary-format fix must
not force a fresh, paid re-judgment. Everything that actually changes what
the model is asked to evaluate still busts the cache.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

from job_intel import JobIntel

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

base_role = {
    "id": "acme-x", "org": "Acme", "title": "Head of Measurement",
    "location": "New York, NY", "comp_range": None, "notes": None,
    "jd": "Own incrementality and MMM for the ads business.",
}

fp_base = JobIntel.fingerprint(base_role)

with_notes = dict(base_role, notes="Reached out on LinkedIn 9/12")
check("adding Notes does NOT change the fingerprint", JobIntel.fingerprint(with_notes) == fp_base)

with_salary = dict(base_role, comp_range="150k-190k")
check("adding Salary does NOT change the fingerprint", JobIntel.fingerprint(with_salary) == fp_base)

with_both_edited = dict(base_role, notes="Different note entirely", comp_range="200k-250k")
check("editing both Notes and Salary still does not change the fingerprint",
      JobIntel.fingerprint(with_both_edited) == fp_base)

# The judge's actual prompt text still SEES notes/salary - only the fingerprint ignores them.
block_with_notes = JobIntel._role_block(with_notes)
check("the judge's prompt text still includes Notes", "Reached out on LinkedIn" in block_with_notes)
block_with_salary = JobIntel._role_block(with_salary)
check("the judge's prompt text still includes Salary", "150k-190k" in block_with_salary)

# Things that SHOULD still bust the cache:
diff_title = dict(base_role, title="Director of Measurement")
check("a different title DOES change the fingerprint", JobIntel.fingerprint(diff_title) != fp_base)

diff_org = dict(base_role, org="Widgets Inc")
check("a different org DOES change the fingerprint", JobIntel.fingerprint(diff_org) != fp_base)

diff_location = dict(base_role, location="Remote")
check("a different location DOES change the fingerprint", JobIntel.fingerprint(diff_location) != fp_base)

diff_jd = dict(base_role, jd="Completely different job description text.")
check("a different JD DOES change the fingerprint", JobIntel.fingerprint(diff_jd) != fp_base)

# Category fingerprint was never affected by notes/comp_range in the first place.
archetypes = [{"id": "01", "label": "Test Archetype", "description": "x"}]
cat_fp_base = JobIntel.cat_fingerprint(base_role, archetypes)
cat_fp_notes = JobIntel.cat_fingerprint(with_notes, archetypes)
check("category fingerprint was already unaffected by Notes/Salary",
      cat_fp_base == cat_fp_notes)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

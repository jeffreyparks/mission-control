"""Learned preferences: which decisions count, when a group becomes a candidate,
that counts come from the evidence and not the model, and that only an approved
me/learned.md ever reaches the fit prompt."""
import sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import pandas as pd
import preferences as prefs
from context import learned_block
from job_intel import JobIntel
from store import COLUMN_MAP, Store

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

check("title levels", [prefs.title_level(t) for t in
      ["VP, Data", "Senior Director, Analytics", "Staff Data Scientist", "Quant Analytics Associate",
       "Senior Data Scientist", "Data Scientist"]]
      == ["VP and above", "Director", "Principal / Staff / Lead", "Junior", "Senior IC",
          "Unspecified level"])
check("industry from sector", prefs.industry("Finance - Banking") == "Finance"
      and prefs.industry("Unknown - Unknown") is None and prefs.industry(None) is None)

work = Path(tempfile.mkdtemp())
for sub in ("artifacts/jobs", "data", "me"):
    (work / sub).mkdir(parents=True)
(work / "me/profile.md").write_text("# Career Goals\n\nMarketing science leader.\n")

cols = list(COLUMN_MAP.keys())
def row(org, title, status, sector, function, reason=None, rec="apply", outcome=None, notes=None):
    return {**{c: None for c in cols}, "Org": org, "Title": title, "Status": status,
            "Sector": sector, "Function": function, "Close Reason": reason,
            "Recommendation": rec, "Outcomes": outcome, "Notes": notes, "Fit Score": 70}

rows = []
# Six banks passed on for industry, all rated apply by the model.
for i in range(6):
    rows.append(row(f"Bank{i}", "Director, Data Science", "04 Closed", "Finance - Banking",
                    "Data Science & ML", "Not a fit - industry", notes="Wrong sector for me"))
# Tech roles pursued - applied, researching, or heard back from the employer.
# The model had said skip on three of them: overrides.
for i in range(3):
    rows.append(row(f"Tech{i}", "Director, Data Science", "03 Applied", "Tech - SaaS",
                    "Data Science & ML", rec="skip"))
rows.append(row("Tech3", "Director, Data Science", "02 Researching", "Tech - SaaS", "Data Science & ML"))
rows.append(row("Tech4", "Director, Data Science", "04 Closed", "Tech - SaaS", "Data Science & ML",
                outcome="Rejected (3d)"))
# Retail passed on five times, but the model had said skip every time: a real
# pattern the model already follows, so it must not become a candidate.
for i in range(5):
    rows.append(row(f"Shop{i}", "Director, Analytics", "04 Closed", "Retail - E-commerce",
                    "Data & Analytics", "Comp", rec="skip"))
# Twelve engineering passes with only two overrides: under the share bar, so
# the model already agrees in all but name.
for i in range(12):
    rows.append(row(f"Eng{i}", "Software Engineer", "04 Closed", "Energy - Utilities",
                    "Engineering", "Not a fit - function", rec="apply" if i < 2 else "skip"))
# None of these say anything about what you want: all must be ignored.
rows.append(row("Media0", "Director", "04 Closed", "Media - News", "Marketing", "Chose another role at org"))
rows.append(row("Media1", "Director", "04 Closed", "Media - News", "Marketing", "Posting closed"))
rows.append(row("Media2", "Director", "04 Closed", "Media - News", "Marketing"))            # no reason
rows.append(row("Media3", "Director", "00 New find", "Media - News", "Marketing"))
store = Store(work)
store.save_df(pd.DataFrame(rows, columns=cols), actor="test-seed")

decisions = prefs.load_decisions(store)
kinds = {d["org"]: d["decision"] for d in decisions}
check("preference-reason closes count as passed", all(kinds.get(f"Bank{i}") == "passed" for i in range(6)))
check("applied, researching and employer outcomes count as pursued",
      all(kinds.get(f"Tech{i}") == "pursued" for i in range(5)), str(kinds))
check("neutral, unexplained and undecided rows are ignored",
      not any(org.startswith("Media") for org in kinds), str(kinds))

# The window: a decision last touched long ago drops out.
later = datetime.now() + timedelta(days=prefs.WINDOW_DAYS + 5)
check("decisions outside the window are ignored", prefs.load_decisions(store, now=later) == [])

ev = prefs.build_evidence(decisions)
check("baseline", ev["decisions"] == 28 and ev["pursued"] == 5, str(ev["decisions"]))
by_value = {(c["dimension"], c["value"]): c for c in ev["candidates"]}
fin = by_value.get(("industry", "Finance"))
check("finance becomes an avoid candidate", fin and fin["lean"] == "avoid", str(list(by_value)))
check("disagreement with the model is counted", fin and fin["passed_though_model_liked"] == 6)
check("notes travel with the candidate", fin and fin["sample_notes"]
      and "Wrong sector for me" in fin["sample_notes"][0])
tech = by_value.get(("industry", "Tech"))
check("tech becomes a seek candidate", tech and tech["lean"] == "seek", str(tech))
check("a mixed group is never an avoid",
      by_value.get(("function", "Data Science & ML"), {}).get("lean") != "avoid")
agreed = {(c["dimension"], c["value"]) for c in ev["agreed"]}
check("a pattern the model already follows is not a candidate",
      ("industry", "Retail") not in by_value and ("industry", "Retail") in agreed, str(agreed))
check("a few overrides in a big agreeing group are still agreement",
      ("function", "Engineering") not in by_value and ("function", "Engineering") in agreed)
check("the unspecified-level leftover is never a group",
      not any(v == prefs.UNSPECIFIED_LEVEL for _d, v in list(by_value) + list(agreed)))

# Too few decisions: nothing is a candidate.
small = prefs.build_evidence(decisions[:4])
check("below MIN_DECISIONS nothing leans", small["candidates"] == [])


class FakeLLM:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []
    def complete_json(self, prompt, tag=None, validate=None, max_tokens=None, **_kw):
        self.prompts.append(prompt)
        self.max_tokens = max_tokens
        assert validate(self.answer), "fake answer must pass the validator"
        return self.answer

fin_id, tech_id = fin["id"], tech["id"]
answer = [{"id": fin_id, "keep": True,
           "statement": "Treat roles at banks as a weak fit whatever the function."},
          {"id": tech_id, "keep": False, "why_dropped": "already in the profile"}]
for c in ev["candidates"]:
    if c["id"] not in (fin_id, tech_id):
        answer.append({"id": c["id"], "keep": False, "why_dropped": "narrower than Finance"})
fake = FakeLLM(answer)
path = prefs.write_draft(work, llm=fake)
draft = path.read_text()
check("the verdict budget scales with the candidates",
      fake.max_tokens == prefs.output_budget(len(ev["candidates"]),
                                             per_item=prefs.VERDICT_TOKENS_PER_CANDIDATE,
                                             overhead=prefs.VERDICT_TOKENS_OVERHEAD),
      str(fake.max_tokens))
check("a big candidate list gets more than the 4096 default",
      prefs.output_budget(40, per_item=prefs.VERDICT_TOKENS_PER_CANDIDATE,
                          overhead=prefs.VERDICT_TOKENS_OVERHEAD) > 4096)
check("draft written, learned.md untouched", path.name == "learned.draft.md"
      and not (work / "me/learned.md").exists())
check("kept statement in the draft", "Treat roles at banks as a weak fit" in draft)
check("evidence counts come from the data", "passed 6, pursued 0 of 6" in draft, draft)
check("dropped candidates are recorded with why", "already in the profile" in draft)
check("patterns the model follows are listed as left out",
      "already follows" in draft and "industry = Retail" in draft)

# The model's call is read as of each decision, not today. A later refresh
# that flips the banks to skip must not turn the overrides into agreement.
with store.connect() as conn:
    conn.execute("UPDATE changes SET ts='2026-01-01T00:00:00' WHERE field='status'")
    conn.execute("INSERT INTO changes(role_id,field,old_value,new_value,actor,ts) "
                 "SELECT id,'status','00 New find',status,'dashboard','2026-01-01T00:00:00' FROM roles")
    for rid in [r["id"] for r in conn.execute("SELECT id FROM roles WHERE org LIKE 'Bank%'")]:
        conn.execute("UPDATE roles SET recommendation='skip' WHERE id=?", (rid,))
        conn.execute("INSERT INTO changes(role_id,field,old_value,new_value,actor,ts) "
                     "VALUES(?,?,?,?,?,?)", (rid, "recommendation", "apply", "skip", "pipeline",
                                             "2026-02-01T00:00:00"))
later_ev = prefs.build_evidence(prefs.load_decisions(store, now=datetime(2026, 3, 1)))
later_fin = {(c["dimension"], c["value"]): c for c in later_ev["candidates"]}.get(("industry", "Finance"))
check("overrides are judged against the call at decision time",
      later_fin is not None and later_fin["passed_though_model_liked"] == 6, str(later_fin))
check("recommendation_at", prefs.recommendation_at(
      [("2026-02-01", "apply", "skip")], "2026-01-01", "skip") == "apply"
      and prefs.recommendation_at([("2026-02-01", "apply", "skip")], "2026-03-01", "skip") == "skip"
      and prefs.recommendation_at([], "2026-01-01", "research") == "research")

validate = prefs.make_validator(ev)
check("validator rejects unknown ids", not validate([{"id": "c99", "keep": True, "statement": "x"}]))
check("validator rejects a kept item with no statement", not validate([{"id": fin_id, "keep": True}]))

# Nothing reaches the fit prompt until approval.
check("an unapproved draft is not read", learned_block(work) == "")
check("fit prompt has no learned section before approval",
      "LEARNED PREFERENCES" not in JobIntel(work)._fit_prefix())

prefs.approve(work)
block = learned_block(work)
check("approved preferences are read", "Treat roles at banks as a weak fit" in block)
check("comments and counts are stripped for the judge", "<!--" not in block and "passed 6" not in block)
check("fit prompt carries the approved section",
      "Treat roles at banks as a weak fit" in JobIntel(work)._fit_prefix())

(work / "me/learned.draft.md").write_text("# Learned preferences\n\n- A newer draft.\n")
prefs.approve(work)
check("approving again keeps the previous copy",
      "Treat roles at banks" in (work / "me/learned.prev.md").read_text()
      and "A newer draft" in (work / "me/learned.md").read_text())

due, _why = prefs.draft_is_due(work)
check("a fresh draft is not due", not due)
check("an old draft is due", prefs.draft_is_due(work, now=datetime.now() + timedelta(days=8))[0])

if fails:
    print(f"\n{len(fails)} failure(s): {fails}")
    sys.exit(1)
print("\nall preferences checks passed")

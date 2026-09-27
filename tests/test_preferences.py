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
                    "Data Science & ML", "Not a fit - industry", notes="Industry fit"))
# Tech roles pursued - applied, researching, or heard back from the employer.
for i in range(3):
    rows.append(row(f"Tech{i}", "Director, Data Science", "03 Applied", "Tech - SaaS", "Data Science & ML"))
rows.append(row("Tech3", "Director, Data Science", "02 Researching", "Tech - SaaS", "Data Science & ML"))
rows.append(row("Tech4", "Director, Data Science", "04 Closed", "Tech - SaaS", "Data Science & ML",
                outcome="Rejected (3d)"))
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
check("baseline", ev["decisions"] == 11 and ev["pursued"] == 5, str(ev["decisions"]))
by_value = {(c["dimension"], c["value"]): c for c in ev["candidates"]}
fin = by_value.get(("industry", "Finance"))
check("finance becomes an avoid candidate", fin and fin["lean"] == "avoid", str(list(by_value)))
check("disagreement with the model is counted", fin and fin["passed_though_model_liked"] == 6)
check("notes travel with the candidate", fin and fin["sample_notes"]
      and "Industry fit" in fin["sample_notes"][0])
tech = by_value.get(("industry", "Tech"))
check("tech becomes a seek candidate", tech and tech["lean"] == "seek", str(tech))
check("a mixed group is not a candidate", ("function", "Data Science & ML") not in by_value)

# Too few decisions: nothing is a candidate.
small = prefs.build_evidence(decisions[:4])
check("below MIN_DECISIONS nothing leans", small["candidates"] == [])


class FakeLLM:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []
    def complete_json(self, prompt, tag=None, validate=None, **_kw):
        self.prompts.append(prompt)
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
check("draft written, learned.md untouched", path.name == "learned.draft.md"
      and not (work / "me/learned.md").exists())
check("kept statement in the draft", "Treat roles at banks as a weak fit" in draft)
check("evidence counts come from the data", "passed 6, pursued 0 of 6" in draft, draft)
check("dropped candidates are recorded with why", "already in the profile" in draft)

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

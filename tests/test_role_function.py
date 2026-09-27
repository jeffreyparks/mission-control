"""Function tagging: every role gets a function, cached by fingerprint, and the
tracker's Function column follows. Separate from Role Cat, which a user-set
value takes out of the categoriser entirely - function tags cover those too."""
import sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import pandas as pd
from job_intel import JobIntel, ROLE_FUNCTIONS, make_function_validator
from store import COLUMN_MAP, Store

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

work = Path(tempfile.mkdtemp())
for sub in ("config", "artifacts/jobs", "data", "me"):
    (work / sub).mkdir(parents=True)

cols = list(COLUMN_MAP.keys())
rows = [
    {**{c: None for c in cols}, "Org": "BigBank", "Title": "Senior Data Scientist", "Status": "01 Open"},
    {**{c: None for c in cols}, "Org": "Acme", "Title": "FP&A Manager", "Status": "04 Closed",
     "Role Cat": "01 Something"},   # user-categorised: still gets a function
]
Store(work).save_df(pd.DataFrame(rows, columns=cols), actor="test-seed")

intel = JobIntel(work)
df = intel.load_tracker()
roles = intel.tracker_roles(df)
ids = [r["id"] for r in roles]

calls = []
def fake(prompt, tag="generic", validate=None, **_kw):
    calls.append(prompt)
    answer = [{"id": ids[0], "function": "Data Science & ML"},
              {"id": ids[1], "function": "Finance & Accounting"}]
    check("validator accepts the answer", validate(answer))
    return answer
intel.llm.complete_json = fake

sent = intel.tag_functions(roles)
check("both roles sent, including the user-categorised one", sent == 2, str(sent))
check("prompt separates function from employer industry",
      "Judge the work, not the employer" in calls[0])
check("tags set on the role dicts",
      [r["role_function"] for r in roles] == ["Data Science & ML", "Finance & Accounting"],
      str([r.get("role_function") for r in roles]))

# Second pass: same text, same fingerprint - no model call.
again = intel.tracker_roles(df)
check("second pass is fully cached", intel.tag_functions(again) == 0 and len(calls) == 1)
check("cached tags restored", again[0]["role_function"] == "Data Science & ML")

# A changed title changes the fingerprint and is re-tagged.
again[0]["title"] = "Staff Data Scientist"
intel.llm.complete_json = lambda prompt, **kw: [{"id": ids[0], "function": "Data Science & ML"}]
check("edited role is re-tagged", intel.tag_functions(again) == 1)

# An invented function is not stored, and not cached.
fresh = intel.tracker_roles(df)
fresh[1]["title"] = "Money Wizard"
intel.llm.complete_json = lambda prompt, **kw: [{"id": ids[1], "function": "Wizardry"}]
intel.tag_functions(fresh[1:])
check("invented function left blank", fresh[1]["role_function"] is None)
check("blank answer not cached", intel.load_function_cache()[ids[1]]["role_function"] != "Wizardry")

validate = make_function_validator([{"id": "a"}, {"id": "b"}])
check("validator rejects out-of-vocabulary answers",
      not validate([{"id": "a", "function": "Wizardry"}, {"id": "b", "function": "Magic"}]))
check("validator accepts wrapped lists",
      validate({"roles": [{"id": "a", "function": ROLE_FUNCTIONS[0]},
                          {"id": "b", "function": ROLE_FUNCTIONS[1]}]}))

# The tracker column follows via update_tracker.
intel.update_tracker(df, roles)
stored = Store(work).to_df()
check("Function column written to the tracker",
      stored["Function"].tolist() == ["Data Science & ML", "Finance & Accounting"],
      str(stored["Function"].tolist()))
check("user Role Cat untouched", stored.iloc[1]["Role Cat"] == "01 Something")

if fails:
    print(f"\n{len(fails)} failure(s): {fails}")
    sys.exit(1)
print("\nall role-function checks passed")

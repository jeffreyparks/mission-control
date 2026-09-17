"""Category auto-accept: the model's first suggestion becomes Role Cat
directly, with no separate approval step - still fully editable afterward."""
import shutil, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import pandas as pd
from job_intel import JobIntel, load_archetypes
from llm import LLM

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

work = Path(tempfile.mkdtemp())
(work / "config").mkdir(parents=True)
(work / "artifacts/jobs").mkdir(parents=True)
(work / "data").mkdir(parents=True)
(work / "me").mkdir(parents=True, exist_ok=True)
shutil.copy2(REPO / "templates/me/profile.md", work / "me/profile.md")

cols = list(__import__("store").COLUMN_MAP.keys())
rows = [
    {**{c: None for c in cols}, "Org": "Acme", "Title": "Measurement Lead", "Status": "01 Open"},
    {**{c: None for c in cols}, "Org": "Widgets", "Title": "Support Rep", "Status": "01 Open",
     "Role Cat": "06 Head of AI at Non-AI Native"},  # user already set this one
]
pd.DataFrame(rows, columns=cols).to_excel(work / "artifacts/jobs/org-roles-tracker.xlsx", index=False)

intel = JobIntel(work)
df = intel.load_tracker()
roles = intel.tracker_roles(df)
archetypes = load_archetypes(work, df)
check("archetypes loaded from me/profile.md", len(archetypes) > 0, str(len(archetypes)))

first_id = archetypes[0]["id"]
first_label = archetypes[0]["label"]

# Stub the LLM: role 0 (blank Role Cat) gets a confident real suggestion;
# role 1 (already has Role Cat) must never even be sent to the model.
def fake_complete_json(prompt, tag="generic", force=False, validate=None, **kwargs):
    check("the already-categorised role is never included in the prompt",
          "Support Rep" not in prompt)
    return [{"id": roles[0]["id"], "archetype": first_id, "confidence": "high",
             "reason": "direct title match"}]
intel.llm.complete_json = fake_complete_json

made = intel.categorize_roles(roles, archetypes)

check("one role sent to the model", made == 1, str(made))
check("role 0 auto-accepted: Role Cat is set, not just suggested",
      roles[0]["role_cat"] == first_label, str(roles[0].get("role_cat")))
check("the suggestion fields are still recorded too (for the tooltip)",
      roles[0]["suggested_role_cat"] == first_label)
check("role 1's user-set Role Cat is completely untouched",
      roles[1]["role_cat"] == "06 Head of AI at Non-AI Native")
check("role 1 was never even given a suggestion (it was skipped entirely)",
      "suggested_role_cat" not in roles[1] or roles[1].get("suggested_role_cat") is None)

# ---------------- persists to the tracker, respecting existing values ----------------
df, added = intel.append_new_finds(df, roles)  # no-op: both roles already have row_index
intel.update_tracker(df, roles)

reloaded = pd.read_excel(work / "artifacts/jobs/org-roles-tracker.xlsx")
acme_row = reloaded[reloaded["Org"] == "Acme"].iloc[0]
widgets_row = reloaded[reloaded["Org"] == "Widgets"].iloc[0]
check("tracker Role Cat is auto-filled for the previously-blank row",
      acme_row["Role Cat"] == first_label, str(acme_row["Role Cat"]))
check("tracker Role Cat is unchanged for the user-set row",
      widgets_row["Role Cat"] == "06 Head of AI at Non-AI Native", str(widgets_row["Role Cat"]))
check("the suggested-category column clears once accepted",
      pd.isna(acme_row["Role Cat (suggested)"]) or acme_row["Role Cat (suggested)"] in (None, ""),
      str(acme_row["Role Cat (suggested)"]))

# ---------------- second run: no LLM call, still respects a later user edit ------------
def _boom(*a, **k):
    raise AssertionError("categorize_roles must not call the model again once cached")
intel2 = JobIntel(work)
intel2.llm.complete_json = _boom
df2 = intel2.load_tracker()
roles2 = intel2.tracker_roles(df2)
archetypes2 = load_archetypes(work, df2)
made2 = intel2.categorize_roles(roles2, archetypes2)
check("second run makes no LLM call (already has Role Cat, skipped up front)", made2 == 0, str(made2))

# A user can still change an auto-accepted category by hand afterward.
reloaded.loc[reloaded["Org"] == "Acme", "Role Cat"] = "02 Field CTO / Head AI Solutions at Vendor"
reloaded.to_excel(work / "artifacts/jobs/org-roles-tracker.xlsx", index=False)
intel3 = JobIntel(work)
intel3.llm.complete_json = _boom
df3 = intel3.load_tracker()
roles3 = intel3.tracker_roles(df3)
acme3 = [r for r in roles3 if r["org"] == "Acme"][0]
check("a later manual override of an auto-accepted category is respected",
      acme3["role_cat"] == "02 Field CTO / Head AI Solutions at Vendor", str(acme3["role_cat"]))

shutil.rmtree(work, ignore_errors=True)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

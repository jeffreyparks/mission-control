"""Close Reason: the migration, the dashboard vocabulary, and the notes backfill.

Covers:
  - an older database without the column gains it, empty, without losing rows
  - the dashboard accepts a reason from the vocabulary (single and bulk) and
    refuses anything else
  - the pipeline's save_df leaves a user-set reason alone
  - the backfill only picks closed rows with real notes and no reason yet,
    skips pipeline-written notes, and never overwrites an existing reason
"""
import json, sqlite3, sys, tempfile, threading, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "scripts"))

import pandas as pd
import requests

import dashboard as srv
import backfill_close_reason as bf
from store import CLOSE_REASONS, COLUMN_MAP, PREFERENCE_CLOSE_REASONS, Store

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

cols = list(COLUMN_MAP.keys())

# ---- migration: a database created before the column existed -------------
old = Path(tempfile.mkdtemp())
(old / "data").mkdir()
conn = sqlite3.connect(old / "data/mission-control.db")
conn.execute("CREATE TABLE roles (id TEXT PRIMARY KEY, row_order INTEGER, org TEXT, "
             "title TEXT, status TEXT, notes TEXT)")
conn.execute("INSERT INTO roles VALUES ('acme-pm', 0, 'Acme', 'PM', '04 Closed', 'x')")
conn.commit()
conn.close()

migrated = Store(old)
with migrated.connect() as c:
    names = {r["name"] for r in c.execute("PRAGMA table_info(roles)")}
check("migration adds close_reason", "close_reason" in names)
check("migration adds every mapped column", set(COLUMN_MAP.values()) <= names,
      str(set(COLUMN_MAP.values()) - names))
check("migration keeps existing rows", migrated.count() == 1)
Store(old)   # second open must be a no-op, not an error
check("migration is idempotent", migrated.count() == 1)

check("preference reasons are a subset of the vocabulary",
      PREFERENCE_CLOSE_REASONS <= set(CLOSE_REASONS))
check("neutral reasons are not preference signal",
      not {"Chose another role at org", "Posting closed", "Other"} & PREFERENCE_CLOSE_REASONS)

# ---- dashboard -------------------------------------------------------------
work = Path(tempfile.mkdtemp())
for sub in ("artifacts/jobs", "artifacts/html", "data", "me"):
    (work / sub).mkdir(parents=True)
(work / "artifacts/html/job-tracker.html").write_text("<html>stub</html>")
rows = [
    {**{c: None for c in cols}, "Org": "Acme", "Title": "Head of Data", "Status": "01 Open"},
    {**{c: None for c in cols}, "Org": "Beta", "Title": "FP&A Lead", "Status": "01 Open"},
    {**{c: None for c in cols}, "Org": "Gamma", "Title": "Analyst", "Status": "01 Open"},
]
store = Store(work)
store.save_df(pd.DataFrame(rows, columns=cols), actor="test-seed")
ids = list(store.to_df()["_id"])

httpd = srv.serve(port=8798, base=work, quiet=True)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)
API = "http://127.0.0.1:8798"

try:
    editable = requests.get(f"{API}/api/health", timeout=5).json()["editable"]
    check("health advertises close_reason", editable.get("close_reason") == [""] + CLOSE_REASONS,
          str(editable.get("close_reason")))

    body = requests.post(f"{API}/api/role/{ids[0]}",
                         json={"field": "close_reason", "value": "Not a fit - function"},
                         timeout=5).json()
    check("single close_reason write succeeds", body.get("ok") and body.get("changed"), str(body))

    r = requests.post(f"{API}/api/role/{ids[0]}",
                      json={"field": "close_reason", "value": "I just didn't like it"}, timeout=5)
    check("reason outside the vocabulary is refused", r.status_code == 400, r.text)

    body = requests.post(f"{API}/api/roles/bulk",
                         json={"ids": ids[1:], "field": "close_reason", "value": "Comp"},
                         timeout=5).json()
    check("bulk close_reason write succeeds", body.get("ok") and body.get("changed") == 2, str(body))

    df = store.to_df().set_index("_id")
    check("reasons persisted", df.loc[ids[0], "Close Reason"] == "Not a fit - function"
          and df.loc[ids[1], "Close Reason"] == "Comp", str(df["Close Reason"].tolist()))
    check("writes are in the change log",
          sum(1 for h in store.history(limit=20) if h["field"] == "close_reason") == 3)

    # The pipeline round-trips the frame it loaded; a user-set reason survives.
    frame = store.to_df()
    frame.loc[frame["_id"] == ids[0], "Fit Score"] = 55
    store.save_df(frame, actor="pipeline")
    check("pipeline save keeps the reason",
          store.to_df().set_index("_id").loc[ids[0], "Close Reason"] == "Not a fit - function")

    csv = pd.read_csv(store.export_csv())
    check("csv export carries the column", "Close Reason" in csv.columns)
finally:
    httpd.shutdown()
    httpd.server_close()

# ---- backfill ---------------------------------------------------------------
bw = Path(tempfile.mkdtemp())
seed = [
    ("Acme", "DS", "04 Closed", "Deprior since applied to other roles", None),
    ("Beta", "FP&A", "04 Closed", "Closing due to gaps and location", None),
    ("Gamma", "PM", "04 Closed", "Auto-discovered. Location: SF", None),     # pipeline note
    ("Delta", "DS", "04 Closed", None, None),                                # no note
    ("Eps", "DS", "04 Closed", "Pay too low", "Comp"),                       # already set
    ("Zeta", "DS", "01 Open", "Looks interesting", None),                    # not closed
]
bstore = Store(bw)
bstore.save_df(pd.DataFrame(
    [{**{c: None for c in cols}, "Org": o, "Title": t, "Status": s, "Notes": n, "Close Reason": cr}
     for o, t, s, n, cr in seed], columns=cols), actor="test-seed")

picked = bf.candidates(bstore)
check("backfill picks only closed rows with real notes and no reason",
      sorted(r["org"] for r in picked) == ["Acme", "Beta"], str([r["org"] for r in picked]))

prompt = bf.build_prompt(picked)
check("prompt lists every reason", all(f'"{reason}"' in prompt for reason in CLOSE_REASONS))

validate = bf.make_validator(2)
check("validator accepts vocabulary answers",
      validate([{"i": 0, "reason": "Chose another role at org"}, {"i": 1, "reason": "none"}]))
check("validator rejects invented reasons",
      not validate([{"i": 0, "reason": "Vibes"}, {"i": 1, "reason": "Also vibes"}]))


class FakeLLM:
    def complete_json(self, prompt, **_kw):
        return [{"i": 0, "reason": "Chose another role at org"},
                {"i": 1, "reason": "Not a fit - skills gap"},
                {"i": 7, "reason": "Comp"}]          # out of range: ignored

found = bf.classify(FakeLLM(), picked)
by_org = {r["id"]: r["org"] for r in picked}
check("classify maps answers back to role ids",
      {by_org[k]: v for k, v in found.items()}
      == {"Acme": "Chose another role at org", "Beta": "Not a fit - skills gap"}, str(found))

# Run main() end to end against the fake, then confirm nothing it should not touch moved.
bf.LLM = lambda _ws: FakeLLM()
bf.workspace.resolve = lambda _user=None: bw
sys.argv = ["backfill_close_reason.py"]
bf.main()
after = bstore.to_df().set_index("Org")
check("backfill writes the classified reasons",
      after.loc["Acme", "Close Reason"] == "Chose another role at org"
      and after.loc["Beta", "Close Reason"] == "Not a fit - skills gap")
check("backfill never overwrites a set reason", after.loc["Eps", "Close Reason"] == "Comp")
check("backfill leaves unknowns blank",
      pd.isna(after.loc["Delta", "Close Reason"]) and pd.isna(after.loc["Gamma", "Close Reason"]))
check("backfill writes are attributed",
      all(h["actor"] == bf.ACTOR for h in bstore.history(limit=20) if h["field"] == "close_reason"))
check("second run finds nothing to do", bf.candidates(bstore) == [])

if fails:
    print(f"\n{len(fails)} failure(s): {fails}")
    sys.exit(1)
print("\nall close-reason checks passed")

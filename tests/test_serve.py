"""API tests for serve.py. Runs a real server on a temp copy of the data."""
import json, shutil, sys, tempfile, threading, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

import pandas as pd
import requests

import serve as srv
from store import Store

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

work = Path(tempfile.mkdtemp())
(work / "artifacts/jobs").mkdir(parents=True)
(work / "artifacts/html").mkdir(parents=True)
(work / "data").mkdir(parents=True)

cols = list(__import__("store").COLUMN_MAP.keys())
rows = [
    {**{c: None for c in cols}, "Org": "Acme", "Title": "Head of Measurement", "Status": "01 Open", "Priority": 1},
    {**{c: None for c in cols}, "Org": "Beta", "Title": "Director, Analytics", "Status": "03 Applied"},
]
pd.DataFrame(rows, columns=cols).to_excel(work / "artifacts/jobs/org-roles-tracker.xlsx", index=False)
(work / "artifacts/html/job-tracker.html").write_text("<html>stub</html>")

store = Store(work)
store.load(verbose=False)
rid = store.to_df().iloc[0]["_id"]

srv.WEB_ROOT = work / "artifacts/html"
srv.BASE = work
httpd = srv.serve(port=8799, base=work, quiet=True)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)
API = "http://127.0.0.1:8799"

try:
    health = requests.get(f"{API}/api/health", timeout=5).json()
    check("health reports rows", health.get("rows") == 2, str(health.get("rows")))
    check("health advertises editable fields", "status" in health.get("editable", {}))

    body = requests.post(f"{API}/api/role/{rid}", json={"field": "status", "value": "04 Closed"}, timeout=5).json()
    check("status write succeeds", body.get("ok") and body.get("changed"), str(body))
    check("old value returned", body.get("old") == "01 Open", str(body.get("old")))

    fresh = Store(work).to_df()
    check("db holds the new status", fresh.iloc[0]["Status"] == "04 Closed", str(fresh.iloc[0]["Status"]))

    exported = pd.read_excel(work / "artifacts/jobs/org-roles-tracker.xlsx")
    check("xlsx re-exported on write", exported.iloc[0]["Status"] == "04 Closed", str(exported.iloc[0]["Status"]))

    again = requests.post(f"{API}/api/role/{rid}", json={"field": "status", "value": "04 Closed"}, timeout=5).json()
    check("idempotent write reports no change", again.get("changed") is False, str(again))

    log = requests.get(f"{API}/api/changes", timeout=5).json()["changes"]
    dash = [c for c in log if c["actor"] == "dashboard"]
    check("one dashboard entry logged", len(dash) == 1, f"{len(dash)} entries")

    bad_field = requests.post(f"{API}/api/role/{rid}", json={"field": "fit_score", "value": 99}, timeout=5)
    check("uneditable field rejected", bad_field.status_code == 400, str(bad_field.status_code))

    bad_value = requests.post(f"{API}/api/role/{rid}", json={"field": "status", "value": "whatever"}, timeout=5)
    check("value outside the vocabulary rejected", bad_value.status_code == 400, str(bad_value.status_code))

    missing = requests.post(f"{API}/api/role/nope", json={"field": "status", "value": "01 Open"}, timeout=5)
    check("unknown role gives 404", missing.status_code == 404, str(missing.status_code))

    junk = requests.post(f"{API}/api/role/{rid}", data=b"not json", timeout=5)
    check("malformed json gives 400", junk.status_code == 400, str(junk.status_code))

    page = requests.get(f"{API}/job-tracker.html", timeout=5)
    check("static dashboard still served", page.status_code == 200 and "stub" in page.text)

    escape = requests.get(f"{API}/../../../etc/passwd", timeout=5)
    check("path traversal blocked", escape.status_code in (400, 403, 404), str(escape.status_code))

    fit = requests.post(f"{API}/api/role/{rid}", json={"field": "notes", "value": "x"}, timeout=5)
    check("non-allowlisted manual field rejected", fit.status_code == 400, str(fit.status_code))
finally:
    httpd.shutdown()
    httpd.server_close()
    shutil.rmtree(work, ignore_errors=True)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

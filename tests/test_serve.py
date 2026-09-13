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
(work / "config").mkdir(parents=True)

cols = list(__import__("store").COLUMN_MAP.keys())
rows = [
    {**{c: None for c in cols}, "Org": "Acme", "Title": "Head of Measurement", "Status": "01 Open", "Priority": 1},
    {**{c: None for c in cols}, "Org": "Beta", "Title": "Director, Analytics", "Status": "03 Applied"},
]
pd.DataFrame(rows, columns=cols).to_excel(work / "artifacts/jobs/org-roles-tracker.xlsx", index=False)
(work / "artifacts/html/job-tracker.html").write_text("<html>stub</html>")
shutil.copy2(REPO / "config/career-goals.md", work / "config/career-goals.md")

# A minimal but real intel snapshot, so GET /job-tracker.html exercises the
# live-overlay render path instead of falling back to the static stub above.
intel_fixture = {
    "generated_at": "2026-01-01T00:00:00", "date": "2026-01-01",
    "counts": {"apply": 1, "research": 0, "skip": 0}, "llm": "LLM: 0 calls, 0 cached, $0.000",
    "roles": [{
        "id": "acme-head-of-measurement", "store_id": None,  # filled in once the store exists
        "source": "tracker", "org": "Acme", "title": "Head of Measurement", "url": None,
        "location": None, "role_cat": None, "priority": 1, "status": "01 Open",
        "date_opened": None, "date_applied": None, "outcome": None, "outcome_label": None,
        "days_to_outcome": None, "outcome_display": None, "notes": None, "comp_range": None,
        "fit_score": 80, "seniority_read": "x", "why": "x", "top_gaps": [],
        "what_theyre_really_hiring_for": "x", "pitch_angle": "x", "recommendation": "apply",
        "suggested_role_cat": None, "suggestion_confidence": None, "suggestion_reason": None,
        "sector": "Tech - SaaS",
    }],
    "orgs": [],
}

store = Store(work)
store.load(verbose=False)
rid = store.to_df().iloc[0]["_id"]

intel_fixture["roles"][0]["store_id"] = rid
intel_fixture["roles"][0]["id"] = rid
(work / "artifacts/jobs" / "intel-2026-01-01.json").write_text(json.dumps(intel_fixture))

httpd = srv.serve(port=8799, base=work, quiet=True)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)
API = "http://127.0.0.1:8799"

try:
    health = requests.get(f"{API}/api/health", timeout=5).json()
    check("health reports rows", health.get("rows") == 2, str(health.get("rows")))
    check("health advertises editable fields", "status" in health.get("editable", {}))
    check("health reports the server's commit for staleness detection",
          "server_commit" in health, str(health.get("server_commit")))
    check("health reports when this process started",
          "server_started_at" in health, str(health.get("server_started_at")))

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

    # The core regression this feature exists for: a GET must reflect what is
    # actually in the database right now, not whatever was true when the page
    # was last rendered to disk. Set a known value first - earlier tests in
    # this file already wrote to this role, so "the fixture's initial value"
    # is not a safe assumption by this point.
    requests.post(f"{API}/api/role/{rid}", json={"field": "status", "value": "02 Researching"}, timeout=5)
    page0 = requests.get(f"{API}/job-tracker.html", timeout=5)
    check("live tracker page served", page0.status_code == 200)
    check("live page reflects a value set moments ago via the API",
          'data-status="02 Researching"' in page0.text and f'data-id="{rid}"' in page0.text)

    requests.post(f"{API}/api/role/{rid}", json={"field": "status", "value": "04 Closed"}, timeout=5)
    page1 = requests.get(f"{API}/job-tracker.html", timeout=5)
    check("a GET right after a write shows the new value, no rebuild needed",
          f'data-id="{rid}"' in page1.text and 'data-status="04 Closed"' in page1.text)
    check("the now-stale status is gone from the response", 'data-status="02 Researching"' not in page1.text)

    # When there is no intel json at all, the live path has nothing to render
    # and must fall back to whatever static file is on disk rather than 500.
    empty_work = Path(tempfile.mkdtemp())
    for sub in ("artifacts/jobs", "artifacts/html", "data", "config"):
        (empty_work / sub).mkdir(parents=True)
    (empty_work / "artifacts/html/job-tracker.html").write_text("<html>stub, no intel yet</html>")
    pd.DataFrame(rows, columns=cols).to_excel(empty_work / "artifacts/jobs/org-roles-tracker.xlsx", index=False)
    httpd2 = srv.serve(port=8800, base=empty_work, quiet=True)
    threading.Thread(target=httpd2.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
        stub_page = requests.get("http://127.0.0.1:8800/job-tracker.html", timeout=5)
        check("falls back to the static file when there is no intel json",
              stub_page.status_code == 200 and "stub, no intel yet" in stub_page.text)
    finally:
        httpd2.shutdown()
        httpd2.server_close()
        shutil.rmtree(empty_work, ignore_errors=True)

    escape = requests.get(f"{API}/../../../etc/passwd", timeout=5)
    check("path traversal blocked", escape.status_code in (400, 403, 404), str(escape.status_code))

    # notes IS editable (see below); fit_score is genuinely pipeline-owned and must
    # stay rejected regardless of how the free-text allowlist grows.
    fit = requests.post(f"{API}/api/role/{rid}", json={"field": "fit_score", "value": 99}, timeout=5)
    check("non-allowlisted judgement field rejected", fit.status_code == 400, str(fit.status_code))

    check("notes is editable", "notes" in health["editable"])
    check("notes vocabulary is null (free text)", health["editable"]["notes"] is None)
    check("comp_range (Salary) is editable", "comp_range" in health["editable"])

    notes_ok = requests.post(f"{API}/api/role/{rid}", json={"field": "notes", "value": "Follow up next week"}, timeout=5).json()
    check("notes write succeeds", notes_ok.get("changed") and notes_ok.get("new") == "Follow up next week", str(notes_ok))

    notes_clear = requests.post(f"{API}/api/role/{rid}", json={"field": "notes", "value": ""}, timeout=5).json()
    check("notes accepts blank (clear)", notes_clear.get("changed") is True, str(notes_clear))

    salary_ok = requests.post(f"{API}/api/role/{rid}", json={"field": "comp_range", "value": "$150K - $190K"}, timeout=5).json()
    check("Salary write succeeds", salary_ok.get("changed") and salary_ok.get("new") == "$150K - $190K", str(salary_ok))

    too_long = requests.post(f"{API}/api/role/{rid}", json={"field": "notes", "value": "x" * 3000}, timeout=5)
    check("notes over the length cap is rejected", too_long.status_code == 400, str(too_long.status_code))

    non_string = requests.post(f"{API}/api/role/{rid}", json={"field": "notes", "value": 12345}, timeout=5)
    check("non-string free text value is rejected", non_string.status_code == 400, str(non_string.status_code))

    check("priority is editable", "priority" in health["editable"])
    check("recommendation is NOT editable (it is the model's judgement, not a status you set)",
          "recommendation" not in health["editable"])
    check("outcomes is editable", "outcomes" in health["editable"], str(health["editable"].get("outcomes")))
    check("role_cat is editable", "role_cat" in health["editable"], str(health["editable"].get("role_cat")))
    check("role_cat vocabulary is populated from career-goals.md",
          len(health["editable"].get("role_cat", [])) > 1, str(health["editable"].get("role_cat")))

    pri = requests.post(f"{API}/api/role/{rid}", json={"field": "priority", "value": "2"}, timeout=5).json()
    check("priority write succeeds", pri.get("changed") and pri.get("new") == 2.0, str(pri))

    rec = requests.post(f"{API}/api/role/{rid}", json={"field": "recommendation", "value": "apply"}, timeout=5)
    check("writing recommendation is rejected", rec.status_code == 400, str(rec.status_code))

    out_ok = requests.post(f"{API}/api/role/{rid}", json={"field": "outcomes", "value": "Rejected"}, timeout=5).json()
    check("outcomes accepts a canonical label", out_ok.get("changed") and out_ok.get("new") == "Rejected", str(out_ok))

    out_bad = requests.post(f"{API}/api/role/{rid}", json={"field": "outcomes", "value": "Whatever"}, timeout=5)
    check("outcomes rejects a free-text value", out_bad.status_code == 400, str(out_bad.status_code))

    role_cat_bad = requests.post(f"{API}/api/role/{rid}", json={"field": "role_cat", "value": "Not A Real Archetype"}, timeout=5)
    check("role_cat rejects an unknown label", role_cat_bad.status_code == 400, str(role_cat_bad.status_code))

    real_label = health["editable"]["role_cat"][1]
    role_cat_ok = requests.post(f"{API}/api/role/{rid}", json={"field": "role_cat", "value": real_label}, timeout=5).json()
    check("role_cat accepts a real archetype label", role_cat_ok.get("changed") and role_cat_ok.get("new") == real_label, str(role_cat_ok))

    role_cat_clear = requests.post(f"{API}/api/role/{rid}", json={"field": "role_cat", "value": ""}, timeout=5).json()
    check("role_cat accepts blank (clear)", role_cat_clear.get("changed") is True, str(role_cat_clear))
finally:
    httpd.shutdown()
    httpd.server_close()
    shutil.rmtree(work, ignore_errors=True)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

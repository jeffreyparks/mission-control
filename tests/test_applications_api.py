"""Dashboard routes for the Applications tab. Runs a real server on a temp store.

Covers:
  - /api/health advertises the new fields, the date fields and the vocabularies
  - stage, next_action, next_action_due and date_applied through the role route:
    vocabulary, length cap, date format and real calendar dates
  - POST /api/role/<id>/close: three logged writes, blanks leave fields alone,
    a bad value writes nothing
  - contacts and touches: create, validate, update/archive, unknown ids
  - request shape: a non-object body, unknown paths
"""
import sys
import threading
from pathlib import Path

import pandas as pd
import pytest
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

import dashboard as srv                                       # noqa: E402
from store import CLOSE_REASONS, COLUMN_MAP, STAGES, Store    # noqa: E402

COLS = list(COLUMN_MAP.keys())


@pytest.fixture
def api(tmp_path):
    for sub in ("artifacts/html", "artifacts/jobs", "me"):
        (tmp_path / sub).mkdir(parents=True)
    rows = [
        {**{c: None for c in COLS}, "Org": "Acme", "Title": "DS", "Status": "03 Applied",
         "Date Applied": "2026-09-12", "Stage": "Applied", "Outcomes": "Screen call 9/20"},
        {**{c: None for c in COLS}, "Org": "Beta", "Title": "VP Data", "Status": "03 Applied"},
    ]
    store = Store(tmp_path)
    store.save_df(pd.DataFrame(rows, columns=COLS), actor="seed")
    ids = {r["Org"]: r["_id"] for r in store.to_df().to_dict("records")}

    httpd = srv.serve(port=0, base=tmp_path, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        return requests.post(base + path, json=body, timeout=5)

    try:
        yield store, ids, post, base
    finally:
        httpd.shutdown()
        httpd.server_close()


def _role(store, role_id):
    return store.get_role(role_id)


# ---------------------------------------------------------------- health

def test_health_advertises_the_new_fields(api):
    _store, _ids, _post, base = api
    health = requests.get(base + "/api/health", timeout=5).json()
    assert health["editable"]["stage"] == [""] + STAGES
    for field in ("next_action", "next_action_due", "date_applied"):
        assert field in health["editable"] and health["editable"][field] is None
    assert health["date_fields"] == ["date_applied", "next_action_due"]
    assert "hiring_manager" in health["vocab"]["contact_kind"]
    assert health["vocab"]["touch_direction"] == ["out", "in"]


# ---------------------------------------------------------------- role fields

def test_stage_accepts_only_the_vocabulary(api):
    store, ids, post, _ = api
    assert post(f"/api/role/{ids['Acme']}", {"field": "stage", "value": "Final"}).json()["ok"]
    assert _role(store, ids["Acme"])["stage"] == "Final"
    assert post(f"/api/role/{ids['Acme']}", {"field": "stage", "value": "Hired"}).status_code == 400


def test_next_action_is_capped(api):
    _store, ids, post, _ = api
    assert post(f"/api/role/{ids['Acme']}", {"field": "next_action", "value": "Call Hal"}).json()["ok"]
    r = post(f"/api/role/{ids['Acme']}", {"field": "next_action", "value": "x" * 201})
    assert r.status_code == 400


@pytest.mark.parametrize("field", ["next_action_due", "date_applied"])
def test_date_fields_take_real_dates_or_blank(api, field):
    store, ids, post, _ = api
    url = f"/api/role/{ids['Acme']}"
    assert post(url, {"field": field, "value": "2026-10-03"}).json()["ok"]
    assert _role(store, ids["Acme"])[field] == "2026-10-03"
    for bad in ("10/03/2026", "2026-02-30", "2026-10-03T10:00", "soon", 20261003):
        assert post(url, {"field": field, "value": bad}).status_code == 400, bad
    assert post(url, {"field": field, "value": ""}).json()["ok"]
    assert _role(store, ids["Acme"])[field] is None


def test_existing_role_route_still_works(api):
    store, ids, post, _ = api
    assert post(f"/api/role/{ids['Beta']}", {"field": "notes", "value": "hi"}).json()["ok"]
    assert post("/api/role/no-such-role", {"field": "notes", "value": "hi"}).status_code == 404


# ---------------------------------------------------------------- close

def test_close_writes_status_outcome_and_reason(api):
    store, ids, post, _ = api
    body = post(f"/api/role/{ids['Beta']}/close",
                {"outcome": "Ghosted", "close_reason": "Comp"}).json()
    assert body["ok"] and set(body["changes"]) == {"status", "outcomes", "close_reason"}
    row = _role(store, ids["Beta"])
    assert (row["status"], row["outcomes"], row["close_reason"]) == ("04 Closed", "Ghosted", "Comp")
    logged = [c for c in store.history(role_id=ids["Beta"], limit=20) if c["actor"] == "dashboard"]
    assert {c["field"] for c in logged} == {"status", "outcomes", "close_reason"}


def test_close_with_blanks_keeps_what_you_wrote(api):
    store, ids, post, _ = api
    body = post(f"/api/role/{ids['Acme']}/close", {"outcome": "", "close_reason": ""}).json()
    assert body["ok"] and set(body["changes"]) == {"status"}
    row = _role(store, ids["Acme"])
    assert (row["status"], row["outcomes"]) == ("04 Closed", "Screen call 9/20")


def test_close_refuses_bad_values_and_writes_nothing(api):
    store, ids, post, _ = api
    url = f"/api/role/{ids['Beta']}/close"
    assert post(url, {"outcome": "Vanished"}).status_code == 400
    assert post(url, {"close_reason": "Bored"}).status_code == 400
    assert post(url, {"outcome": "Ghosted", "status": "01 Open"}).status_code == 400
    assert _role(store, ids["Beta"])["status"] == "03 Applied"
    assert post("/api/role/no-such-role/close", {"outcome": "Ghosted"}).status_code == 404


def test_close_reason_vocabulary_matches_the_store(api):
    _store, ids, post, _ = api
    assert post(f"/api/role/{ids['Beta']}/close", {"close_reason": CLOSE_REASONS[0]}).json()["ok"]


# ---------------------------------------------------------------- contacts

def test_create_contact(api):
    store, ids, post, _ = api
    body = post("/api/contacts", {"org": "Acme", "name": "Hal Ng", "kind": "hiring_manager",
                                  "url": "https://www.linkedin.com/in/halng",
                                  "email": "hal@acme.example", "role_id": ids["Acme"]}).json()
    assert body["ok"] and body["contact"]["name"] == "Hal Ng"
    assert [c["name"] for c in store.contacts_for("acme")] == ["Hal Ng"]


@pytest.mark.parametrize("payload, status", [
    ({"org": "Acme"}, 400),                                           # no name
    ({"name": "Hal"}, 400),                                           # no org
    ({"org": "Acme", "name": "Hal", "kind": "boss"}, 400),
    ({"org": "Acme", "name": "Hal", "url": "javascript:alert(1)"}, 400),
    ({"org": "Acme", "name": "Hal", "email": "not an email"}, 400),
    ({"org": "Acme", "name": "Hal", "notes": "x" * 1001}, 400),
    ({"org": "Acme", "name": "Hal", "phone": "555"}, 400),
    ({"org": "Acme", "name": 7}, 400),
    ({"org": "Acme", "name": "Hal", "archived": True}, 400),          # not on create
    ({"org": "Acme", "name": "Hal", "role_id": "no-such-role"}, 404),
])
def test_create_contact_validation(api, payload, status):
    store, _ids, post, _ = api
    assert post("/api/contacts", payload).status_code == status
    assert store.contacts_for("Acme", include_archived=True) == []


def test_update_and_archive_contact(api):
    store, _ids, post, _ = api
    cid = post("/api/contacts", {"org": "Acme", "name": "Hal"}).json()["contact"]["id"]

    body = post(f"/api/contact/{cid}", {"title": "Head of Data"}).json()
    assert body["ok"] and body["changed"] and body["contact"]["title"] == "Head of Data"

    assert post(f"/api/contact/{cid}", {"archived": "yes"}).status_code == 400
    assert post(f"/api/contact/{cid}", {"name": ""}).status_code == 400
    assert post(f"/api/contact/{cid}", {}).status_code == 400
    assert post(f"/api/contact/{cid}", {"archived": True}).json()["changed"]
    assert store.contacts_for("Acme") == []
    assert post("/api/contact/9999", {"title": "x"}).status_code == 404


# ---------------------------------------------------------------- touches

def test_create_touch(api):
    store, ids, post, _ = api
    cid = post("/api/contacts", {"org": "Acme", "name": "Hal"}).json()["contact"]["id"]
    body = post("/api/touches", {"role_id": ids["Acme"], "date": "2026-09-20",
                                 "channel": "email", "direction": "in",
                                 "contact_id": cid, "summary": "Wants to talk"}).json()
    assert body["ok"] and body["touch"]["contact_id"] == cid
    assert len(store.touches_for(ids["Acme"])) == 1


@pytest.mark.parametrize("payload, status", [
    ({"date": "2026-09-20"}, 400),                                    # no role
    ({"role_id": "ROLE"}, 400),                                       # no date
    ({"role_id": "ROLE", "date": "yesterday"}, 400),
    ({"role_id": "ROLE", "date": "2026-09-20", "channel": "fax"}, 400),
    ({"role_id": "ROLE", "date": "2026-09-20", "direction": "sideways"}, 400),
    ({"role_id": "ROLE", "date": "2026-09-20", "contact_id": "1"}, 400),
    ({"role_id": "ROLE", "date": "2026-09-20", "contact_id": True}, 400),
    ({"role_id": "ROLE", "date": "2026-09-20", "summary": "x" * 1001}, 400),
    ({"role_id": "ROLE", "date": "2026-09-20", "mood": "good"}, 400),
    ({"role_id": "ROLE", "date": "2026-09-20", "contact_id": 9999}, 404),
    ({"role_id": "no-such-role", "date": "2026-09-20"}, 404),
])
def test_create_touch_validation(api, payload, status):
    store, ids, post, _ = api
    if payload.get("role_id") == "ROLE":
        payload = {**payload, "role_id": ids["Acme"]}
    assert post("/api/touches", payload).status_code == status
    assert store.touches_for(ids["Acme"], include_archived=True) == []


def test_touches_can_only_be_archived(api):
    store, ids, post, _ = api
    tid = post("/api/touches", {"role_id": ids["Acme"], "date": "2026-09-20"}).json()["touch"]["id"]
    assert post(f"/api/touch/{tid}", {"summary": "edited"}).status_code == 400
    assert post(f"/api/touch/{tid}", {"archived": True}).json()["changed"]
    assert store.touches_for(ids["Acme"]) == []
    assert post("/api/touch/9999", {"archived": True}).status_code == 404


# ---------------------------------------------------------------- request shape

def test_body_must_be_an_object(api):
    _store, ids, post, _ = api
    assert post("/api/contacts", ["Acme", "Hal"]).status_code == 400
    assert post(f"/api/role/{ids['Acme']}", "stage").status_code == 400


def test_unknown_paths_are_404(api):
    _store, _ids, post, _ = api
    for path in ("/api/contact/abc", "/api/touch/", "/api/role/", "/api/nothing"):
        assert post(path, {}).status_code == 404, path

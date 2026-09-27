"""Filling Date Applied and Stage: the one-off backfill and the dashboard hook.

Covers:
  - the backfill's order of evidence: change log, then Last Updated, then
    Date Opened, never earlier than Date Opened
  - stage read from Outcomes where it names one, otherwise "Applied"
  - only applied roles with a blank field are touched; a date or stage you set
    is never overwritten, and a second run changes nothing
  - writes are logged under the backfill's actor
  - moving a role to Applied from the dashboard (single or bulk) fills today's
    date and "Applied" only where empty, and re-applying keeps the old date
"""
import sys
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "scripts"))

import backfill_date_applied as bf  # noqa: E402
import dashboard as srv              # noqa: E402
from store import COLUMN_MAP, Store  # noqa: E402

COLS = list(COLUMN_MAP.keys())


def _row(org, title, status="03 Applied", **fields):
    return {**{c: None for c in COLS}, "Org": org, "Title": title, "Status": status, **fields}


def _store(tmp_path, rows):
    store = Store(tmp_path)
    store.save_df(pd.DataFrame(rows, columns=COLS), actor="seed")
    return store, {r["Org"]: r["_id"] for r in store.to_df().to_dict("records")}


def _field(store, role_id, header):
    return store.to_df().set_index("_id").loc[role_id, header]


# ---------------------------------------------------------------- backfill

@pytest.fixture
def seeded(tmp_path):
    store, ids = _store(tmp_path, [
        _row("Logged", "DS", "01 Open", **{"Date Opened": "2026-09-01",
                                           "Last Updated": "2026-09-02"}),
        _row("Updated", "DS", **{"Date Opened": "2026-09-01", "Last Updated": "2026-09-05"}),
        _row("Opened", "DS", **{"Date Opened": "2026-09-03"}),
        _row("Nothing", "DS"),
        _row("Screened", "DS", **{"Date Applied": "2026-08-30", "Outcomes": "Screen call 9/20"}),
        _row("Mine", "DS", **{"Date Applied": "2026-08-01", "Stage": "Final"}),
        _row("Stale", "DS", **{"Date Opened": "2026-09-10", "Last Updated": "2026-09-04"}),
        _row("Closed", "DS", "04 Closed", **{"Date Opened": "2026-09-01"}),
    ])
    # A real record: the dashboard moved this one to Applied on 9/21.
    store.set_field(ids["Logged"], "status", "03 Applied", actor="dashboard")
    with store.connect() as conn:
        conn.execute("UPDATE changes SET ts='2026-09-21T10:57:04' "
                     "WHERE role_id=? AND field='status'", (ids["Logged"],))
    return store, ids


def test_backfill_uses_best_evidence_in_order(seeded):
    store, ids = seeded
    planned = {e["id"]: e for e in bf.plan(store)}

    assert planned[ids["Logged"]]["date_applied"] == "2026-09-21"
    assert planned[ids["Logged"]]["date_source"] == "change log"
    assert planned[ids["Updated"]]["date_applied"] == "2026-09-05"
    assert planned[ids["Updated"]]["date_source"] == "last updated"
    assert planned[ids["Opened"]]["date_applied"] == "2026-09-03"
    assert planned[ids["Opened"]]["date_source"] == "date opened"
    assert planned[ids["Stale"]]["date_applied"] == "2026-09-10"   # clamped to opened
    assert "date_applied" not in planned[ids["Nothing"]]
    assert planned[ids["Nothing"]]["date_source"] == "no evidence"


def test_backfill_reads_stage_from_outcomes(seeded):
    store, ids = seeded
    planned = {e["id"]: e for e in bf.plan(store)}
    assert planned[ids["Screened"]]["stage"] == "Screen"
    assert "date_applied" not in planned[ids["Screened"]]     # already set
    assert planned[ids["Updated"]]["stage"] == "Applied"


def test_backfill_skips_what_is_set_and_what_is_not_applied(seeded):
    store, ids = seeded
    planned = {e["id"] for e in bf.plan(store)}
    assert ids["Mine"] not in planned
    assert ids["Closed"] not in planned


def test_backfill_writes_logged_and_is_idempotent(seeded):
    store, ids = seeded
    assert bf.apply(store, bf.plan(store)) > 0

    assert _field(store, ids["Updated"], "Date Applied") == "2026-09-05"
    assert _field(store, ids["Mine"], "Date Applied") == "2026-08-01"
    assert _field(store, ids["Mine"], "Stage") == "Final"
    actors = {c["actor"] for c in store.history(role_id=ids["Updated"], limit=20)
              if c["field"] in ("date_applied", "stage")}
    assert actors == {bf.ACTOR}

    # Second run: only the role with no evidence for a date is still listed,
    # and it has nothing left to write.
    again = bf.plan(store)
    assert [e["id"] for e in again] == [ids["Nothing"]]
    assert bf.apply(store, again) == 0


# ---------------------------------------------------------------- dashboard hook

@pytest.fixture
def server(tmp_path):
    for sub in ("artifacts/html", "artifacts/jobs", "me"):
        (tmp_path / sub).mkdir(parents=True)
    store, ids = _store(tmp_path, [
        _row("Acme", "DS", "01 Open"),
        _row("Beta", "DS", "01 Open"),
        _row("Gamma", "DS", "04 Closed", **{"Date Applied": "2026-08-01", "Stage": "Screen"}),
    ])
    httpd = srv.serve(port=0, base=tmp_path, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield store, ids, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_moving_to_applied_fills_date_and_stage(server):
    store, ids, api = server
    today = datetime.now().strftime("%Y-%m-%d")
    body = requests.post(f"{api}/api/role/{ids['Acme']}",
                         json={"field": "status", "value": "03 Applied"}, timeout=5).json()
    assert body["ok"] and body["filled"] == {"date_applied": today, "stage": "Applied"}
    assert _field(store, ids["Acme"], "Date Applied") == today
    assert _field(store, ids["Acme"], "Stage") == "Applied"


def test_reapplying_keeps_the_original_date(server):
    store, ids, api = server
    body = requests.post(f"{api}/api/role/{ids['Gamma']}",
                         json={"field": "status", "value": "03 Applied"}, timeout=5).json()
    assert body["ok"] and body["filled"] == {}
    assert _field(store, ids["Gamma"], "Date Applied") == "2026-08-01"
    assert _field(store, ids["Gamma"], "Stage") == "Screen"


def test_other_status_changes_fill_nothing(server):
    store, ids, api = server
    body = requests.post(f"{api}/api/role/{ids['Acme']}",
                         json={"field": "status", "value": "02 Researching"}, timeout=5).json()
    assert body["ok"] and body["filled"] == {}
    assert pd.isna(_field(store, ids["Acme"], "Date Applied"))


def test_bulk_move_to_applied_fills_each_row(server):
    store, ids, api = server
    body = requests.post(f"{api}/api/roles/bulk",
                         json={"ids": [ids["Acme"], ids["Beta"]], "field": "status",
                               "value": "03 Applied"}, timeout=5).json()
    assert body["ok"] and body["changed"] == 2
    assert all(r["filled"].get("stage") == "Applied" for r in body["results"])
    assert _field(store, ids["Beta"], "Stage") == "Applied"

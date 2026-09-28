"""The Applications page: its data, the static and live renders, and the Home card.

Covers:
  - roles grouped by urgency and sorted by due date; the header counts
  - "recently closed" holds only applications (Applied -> Closed) in the window
  - the fit review adds positioning when there is one; the page renders without
  - only http(s) links become hrefs, names are escaped, searches are encoded
  - the timeline merges touches and changes, and marks estimates and confirmations
  - saving an estimated date unchanged confirms it (Store.confirm_field)
  - /applications.html is rendered live, and the Home card shows the counts
"""
import json
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "render"))

import applications as app           # noqa: E402
import build                          # noqa: E402
import dashboard as srv               # noqa: E402
from store import COLUMN_MAP, Store   # noqa: E402

COLS = list(COLUMN_MAP.keys())
TODAY = date(2026, 9, 27)


def ago(days, today=TODAY):
    return (today - timedelta(days=days)).isoformat()


def _row(org, title, status="03 Applied", **fields):
    return {**{c: None for c in COLS}, "Org": org, "Title": title, "Status": status, **fields}


def _workspace(tmp_path, rows, intel_roles=None):
    for sub in ("artifacts/html", "artifacts/jobs", "me"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path)
    store.save_df(pd.DataFrame(rows, columns=COLS), actor="seed")
    ids = {r["Org"]: r["_id"] for r in store.to_df().to_dict("records")}
    if intel_roles is not None:
        roles = [{**r, "store_id": ids[r.pop("org_key")]} for r in intel_roles]
        (tmp_path / "artifacts/jobs/intel-2026-09-27.json").write_text(json.dumps({"roles": roles}))
    return store, ids


def _set_ts(store, role_id, field, ts):
    with store.connect() as conn:
        conn.execute("UPDATE changes SET ts=? WHERE role_id=? AND field=?", (ts, role_id, field))


@pytest.fixture
def ws(tmp_path):
    store, ids = _workspace(tmp_path, [
        _row("Overdue Co", "DS Lead", **{"Date Applied": ago(20), "Stage": "Applied"}),
        _row("Soon Co", "Analytics Head", **{"Date Applied": ago(2), "Stage": "Applied"}),
        _row("Offer Co", "VP Data", **{"Date Applied": ago(40), "Stage": "Offer"}),
        _row("Guess Co", "Director", **{"Date Applied": ago(9), "Stage": "Applied"}),
        _row("Gone Co", "Lead", "01 Open"),
        _row("Swept Co", "Analyst", "01 Open"),
        _row("Old Co", "Manager", "03 Applied"),
    ], intel_roles=[{"org_key": "Soon Co", "fit_score": 71, "pitch_angle": "Lead with the pricing work.",
                     "top_gaps": ["No retail experience"]}])
    store.set_field(ids["Guess Co"], "date_applied", ago(8), actor=app.BACKFILL_ACTOR)
    # Closed from Applied inside the window; bulk-closed from Open; closed long ago.
    store.set_field(ids["Gone Co"], "status", "03 Applied")
    store.set_field(ids["Gone Co"], "status", "04 Closed")
    store.set_field(ids["Swept Co"], "status", "04 Closed")
    store.set_field(ids["Old Co"], "status", "04 Closed")
    _set_ts(store, ids["Old Co"], "status", ago(30) + "T09:00:00")
    return store, ids, tmp_path


# ---------------------------------------------------------------- context

def test_groups_sorted_by_urgency(ws):
    _store, _ids, base = ws
    ctx = build.applications_context(base, today=TODAY)
    order = [(bucket, [a["org"] for a in items]) for bucket, _label, items in ctx["groups"]]
    assert order == [("overdue", ["Overdue Co"]), ("today", ["Guess Co"]),
                     ("week", ["Soon Co"]), ("waiting", ["Offer Co"])]
    assert ctx["counts"] == {"active": 4, "overdue": 1, "soon": 2, "waiting": 1, "estimates": 1}


def test_recently_closed_means_closed_applications(ws):
    _store, _ids, base = ws
    closed = [c["org"] for c in build.applications_context(base, today=date.today())["closed"]]
    assert closed == ["Gone Co"]


def test_positioning_comes_from_the_fit_review(ws):
    _store, _ids, base = ws
    ctx = build.applications_context(base, today=TODAY)
    soon = next(a for _b, _l, items in ctx["groups"] for a in items if a["org"] == "Soon Co")
    assert (soon["fit_score"], soon["pitch_angle"], soon["top_gaps"]) == (
        71, "Lead with the pricing work.", ["No retail experience"])
    assert ctx["has_intel"]


def test_page_renders_without_a_fit_review(tmp_path):
    _store, _ids = _workspace(tmp_path, [_row("Solo Co", "DS", **{"Date Applied": ago(3)})])
    _path, html = build.render_applications(build._env(), write=False, base=tmp_path, today=TODAY)
    assert "Solo Co" in html and "No fit review for this role yet." in html


def test_empty_page(tmp_path):
    _workspace(tmp_path, [_row("Idle Co", "DS", "01 Open")])
    _path, html = build.render_applications(build._env(), write=False, base=tmp_path, today=TODAY)
    assert "No active applications" in html


# ---------------------------------------------------------------- what reaches the html

def test_only_http_links_and_escaped_names(tmp_path):
    store, ids = _workspace(tmp_path, [
        _row("Acme & Sons", "DS", **{"Date Applied": ago(3), "Role Link": "javascript:alert(1)"}),
    ])
    store.add_contact(org="Acme & Sons", name="<b>Dana</b>", url="javascript:alert(2)")
    _path, html = build.render_applications(build._env(), write=False, base=tmp_path, today=TODAY)
    assert "javascript:" not in html
    assert "<b>Dana</b>" not in html and "&lt;b&gt;Dana&lt;/b&gt;" in html
    assert "keywords=Acme%20%26%20Sons%20recruiter" in html


def test_timeline_merges_touches_and_changes(ws):
    store, ids, base = ws
    contact = store.add_contact(org="Guess Co", name="Rae Kim", kind="recruiter")
    store.add_touch(role_id=ids["Guess Co"], date=ago(1), direction="in", channel="email",
                    contact_id=contact["id"], summary="Can we talk?")
    store.confirm_field(ids["Guess Co"], "date_applied")
    ctx = build.applications_context(base, today=TODAY)
    guess = next(a for _b, _l, items in ctx["groups"] for a in items if a["org"] == "Guess Co")
    texts = [e["text"] for e in guess["timeline"]]
    assert "Received · email · from Rae Kim: Can we talk?" in texts
    assert f"Date applied confirmed: {ago(8)}" in texts
    assert any(t.endswith("(estimated)") for t in texts)
    assert guess["s"].action == "Reply"
    assert guess["last_touch"] == "yesterday · in · email · Rae Kim"


# ---------------------------------------------------------------- confirming a date

def test_confirm_field_logs_old_equals_new(ws):
    store, ids, _base = ws
    assert app.date_is_estimate(store.history(role_id=ids["Guess Co"], limit=50))
    assert store.confirm_field(ids["Guess Co"], "date_applied", actor="dashboard") == ago(8)
    latest = store.history(role_id=ids["Guess Co"], limit=1)[0]
    assert (latest["field"], latest["old_value"], latest["new_value"], latest["actor"]) == (
        "date_applied", ago(8), ago(8), "dashboard")
    assert not app.date_is_estimate(store.history(role_id=ids["Guess Co"], limit=50))
    with pytest.raises(ValueError):
        store.confirm_field(ids["Guess Co"], "nonsense")
    with pytest.raises(KeyError):
        store.confirm_field("no-such-role", "date_applied")


# ---------------------------------------------------------------- live and home

@pytest.fixture
def server(ws):
    store, ids, base = ws
    httpd = srv.serve(port=0, base=base, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield store, ids, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_live_page_reflects_writes(server):
    _store, ids, api = server
    page = requests.get(f"{api}/applications.html", timeout=5)
    assert page.status_code == 200 and "Guess Co" in page.text
    assert 'class="on">Applications</a>' in page.text
    requests.post(f"{api}/api/touches", json={"role_id": ids["Soon Co"], "date": date.today().isoformat(),
                                              "direction": "out", "channel": "linkedin",
                                              "summary": "Sent a note"}, timeout=5)
    assert "Sent a note" in requests.get(f"{api}/applications.html", timeout=5).text


def test_saving_the_same_date_confirms_it(server):
    store, ids, api = server
    body = requests.post(f"{api}/api/role/{ids['Guess Co']}",
                         json={"field": "date_applied", "value": ago(8)}, timeout=5).json()
    assert body["ok"] and body["changed"] is False and body["confirmed"] is True
    assert app.suggest_for(store, ids["Guess Co"], today=TODAY).action != "Confirm date applied"

    # Other fields saved unchanged are not "confirmed".
    body = requests.post(f"{api}/api/role/{ids['Guess Co']}",
                         json={"field": "stage", "value": "Applied"}, timeout=5).json()
    assert body["confirmed"] is False


def test_home_card(ws):
    _store, _ids, base = ws
    build.render_index(build._env(), None, None, base=base)
    html = (base / "artifacts/html/index.html").read_text()
    assert 'href="applications.html"' in html and "<h3>Applications</h3>" in html
    summary = build.applications_summary(base, today=TODAY)
    assert summary["active"] == 4 and summary["next"]["org"] == "Overdue Co"

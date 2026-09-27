"""The Profile page: static render, Home card, and the dashboard's live page and
finding triage route. Real server on a temp workspace; no model calls."""
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "render"))

import build  # noqa: E402
from profile_eval import fingerprint  # noqa: E402
from profile_snapshot import make_snapshot  # noqa: E402
from store import Store  # noqa: E402

KEY = "site:https://me.dev/"


def _evaluation(store, created, overall, findings, stale=False):
    snap = make_snapshot("site", KEY, {"hero": "Measurement lead"},
                         [{"kind": "section", "id": "s1", "title": "About",
                           "text": "I design experiments with SQL."}],
                         {"stale": stale, "export_date": "2026-08-01"})
    snap["hash"] = f"h-{created}"
    _, sid = store.save_snapshot_if_changed(snap)
    brief = {"asks": [{"ask": "sql", "roles": 3, "share": 0.6},
                      {"ask": "a/b testing", "roles": 5, "share": 1.0}],
             "with_jd": 5, "pursued": 7, "window_days": 60,
             "persona": {"source": "Career Positioning", "text": "x"}}
    eid = store.add_evaluation({
        "created_at": created, "trigger": "forced", "snapshot_ids": {KEY: sid},
        "brief_hash": "b", "brief": brief,
        "scores": {"overall": overall, "per_source": {KEY: {**overall, "freshness": None}}},
        "shares": {KEY: {"on": 0.75, "adjacent": 0.25, "tangent": 0.0}},
        "per_source": [], "summary": f"Verdict from {created[:10]}.",
    })
    for n, f in enumerate(findings):
        f.setdefault("source_key", KEY)
        f["rank"] = n + 1
        f["fingerprint"] = fingerprint(f)
    store.sync_findings(eid, findings, now=created)
    return eid


def _finding(title, severity="high", dimension="coverage", ask=None, quote=None):
    return {"dimension": dimension, "severity": severity, "title": title, "quote": quote,
            "url": "https://me.dev/", "ask": ask, "ask_roles": 5 if ask else None, "fix": "Do the thing"}


OVERALL = {"coverage": 3, "drift": 4, "consistency": 2, "proof": 2, "register": 3, "freshness": None}


@pytest.fixture
def base(tmp_path):
    (tmp_path / "artifacts/html").mkdir(parents=True)
    return tmp_path


def _render(base):
    return build.render_profile(build._env(), write=False, base=base)[1]


def test_empty_page_before_any_evaluation(base):
    html = _render(base)
    assert "No evaluation yet" in html and 'class="finding"' not in html


def test_page_shows_verdict_scores_brief_drift_and_findings(base):
    store = Store(base)
    _evaluation(store, "2026-09-27T12:00:00", OVERALL,
                [_finding("No A/B testing", ask="a/b testing"),
                 _finding("Tone slips", severity="medium", dimension="register", quote="I design experiments")],
                stale=True)
    html = _render(base)
    assert "Verdict from 2026-09-27." in html
    assert "7 pursued roles, 5 with descriptions" in html
    assert "LinkedIn export is from" not in html                       # a stale flag only means something on LinkedIn
    assert "Career Positioning" in html                                # persona banner
    assert html.count('class="finding"') == 2
    assert "ask: a/b testing (5 roles)" in html
    assert "<blockquote>I design experiments</blockquote>" in html
    assert "75% on · 25% adj · 0% tan" in html
    assert 'title="mentioned"' in html                                  # sql appears on the site
    assert 'href="profile.html" class="on"' in html
    assert "&nbsp;·&nbsp; 1 item</div>" in html                        # item count on the source card


def test_score_changes_against_the_previous_evaluation(base):
    store = Store(base)
    _evaluation(store, "2026-09-20T12:00:00", {**OVERALL, "proof": 1, "drift": 5}, [_finding("Old")])
    _evaluation(store, "2026-09-27T12:00:00", OVERALL, [_finding("New")])
    html = _render(base)
    assert '<span class="delta up">+1</span>' in html                  # proof 1 -> 2
    assert '<span class="delta down">-1</span>' in html                # drift 5 -> 4
    assert "Changes are against the evaluation of 2026-09-20." in html


def test_static_copy_keeps_triage_hidden(base):
    store = Store(base)
    _evaluation(store, "2026-09-27T12:00:00", OVERALL, [_finding("No A/B testing")])
    html = _render(base)
    assert '<body>' in html and 'class="live"' not in html
    assert ".actions{display:none" in html and "body.live .actions{display:flex}" in html
    assert "h.finding_states" in html                                   # buttons wait for the server


def test_home_card(base):
    store = Store(base)
    assert build.profile_summary(base) is None
    _evaluation(store, "2026-09-27T12:00:00", OVERALL,
                [_finding("A"), _finding("B", severity="low")])
    summary = build.profile_summary(base)
    assert summary == {"date": "2026-09-27", "weakest": "consistency", "weakest_score": 2,
                       "open": 2, "high": 1}
    out = build.render_index(build._env(), None, None, base=base)
    html = out.read_text()
    assert 'href="profile.html"' in html and "Weakest:" in html and "LinkedIn keywords" not in html


# ---------- dashboard ----------

@pytest.fixture
def server(base):
    import dashboard as srv
    (base / "artifacts/html/index.html").write_text("<html></html>")
    store = Store(base)
    _evaluation(store, "2026-09-27T12:00:00", OVERALL,
                [_finding("No A/B testing", ask="a/b testing"), _finding("Tone", severity="low")])
    httpd = srv.serve(port=8796, base=base, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield "http://127.0.0.1:8796", store
    httpd.shutdown()
    httpd.server_close()


def test_health_names_finding_states_apart_from_role_fields(server):
    api, _ = server
    health = requests.get(f"{api}/api/health", timeout=5).json()
    assert health["finding_states"] == ["open", "accepted", "dismissed", "fixed"]
    assert "finding_state" not in health["editable"]


def test_triage_route_writes_logs_and_shows_on_the_live_page(server):
    api, store = server
    fid = store.profile_findings()[0]["id"]

    r = requests.post(f"{api}/api/finding/{fid}", json={"state": "dismissed", "note": "Not relevant"}, timeout=5)
    assert r.status_code == 200 and r.json() == {"ok": True, "id": fid, "changed": True,
                                                 "old": "open", "new": "dismissed"}
    row = next(f for f in store.profile_findings(states=None) if f["id"] == fid)
    assert row["state"] == "dismissed" and row["state_note"] == "Not relevant"
    log = store.history(limit=5)
    assert any(c["field"] == f"finding:{fid}:state" and c["actor"] == "dashboard" for c in log)

    page = requests.get(f"{api}/profile.html", timeout=5).text
    assert f'data-id="{fid}" data-state="dismissed" hidden' in page

    again = requests.post(f"{api}/api/finding/{fid}", json={"state": "dismissed", "note": "Not relevant"}, timeout=5)
    assert again.json()["changed"] is False


@pytest.mark.parametrize("path, body, code", [
    ("1", {"state": "archived"}, 400),
    ("1", {"state": "dismissed", "note": "x" * 501}, 400),
    ("1", {"state": "open", "note": 5}, 400),
    ("9999", {"state": "open"}, 404),
    ("abc", {"state": "open"}, 404),
])
def test_triage_route_rejects_bad_requests(server, path, body, code):
    api, _ = server
    r = requests.post(f"{api}/api/finding/{path}", json=body, timeout=5)
    assert r.status_code == code and "error" in r.json()


def test_triage_route_refuses_other_machines(server, monkeypatch):
    import dashboard as srv
    api, store = server
    monkeypatch.setattr(srv.Handler, "_loopback", lambda self: False)
    fid = store.profile_findings()[0]["id"]
    r = requests.post(f"{api}/api/finding/{fid}", json={"state": "fixed"}, timeout=5)
    assert r.status_code == 403
    assert store.profile_findings()[0]["state"] == "open"

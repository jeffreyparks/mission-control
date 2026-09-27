"""Draft rewrites: fields per source, the length retry, the claim check, the draft
and persona files, approval (file, CLI, dashboard), the approved-but-not-published
finding, and the page section. Every model call is stubbed."""
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
import profile_drafts as pd_  # noqa: E402
from profile_snapshot import make_snapshot  # noqa: E402
from store import Store  # noqa: E402

RESUME = "Sr. Director at Initech. Led a team of 5. Managed $40M in media. Built MMM at Globex."


class StubLLM:
    def __init__(self, answers):
        self.answers, self.calls = answers, []
        self.stats = {"misses": 0}

    def complete_json(self, prompt, tag="generic", validate=None, **_):
        self.calls.append((tag, prompt))
        answer = self.answers[tag]
        if isinstance(answer, list):
            answer = answer.pop(0)
        data = answer(prompt) if callable(answer) else answer
        if validate:
            assert validate(data), f"stub answer for {tag} fails validation: {data}"
        return data

    def report(self):
        return "LLM: stub"


def _snaps():
    linkedin = make_snapshot("linkedin", "linkedin:export", {"headline": "Applied AI & ML | MMM"}, [])
    github = make_snapshot("github", "github:me", {"bio": "Measurement", "readme": "Hi."}, [])
    site = make_snapshot("site", "site:https://me.dev/", {"description": "Measurement lead"}, [])
    for n, s in enumerate((linkedin, github, site), 1):
        s["_id"] = n
    return [linkedin, github, site]


def _answer(overrides=None):
    texts = {
        "linkedin:export#headline": ["Sr. Director, Marketing Science | MMM at Globex",
                                     "Led a team of 5 managing $60M across Acme and Initech"],
        "linkedin:export#about": ["I lead measurement teams."],
        "github:me#bio": ["Measurement leader. MMM, causal inference.", "x" * 200],
        "github:me#readme": ["I build measurement systems."],
        "site:https://me.dev/#tagline": ["Measurement that moves budgets", "MMM and causal inference"],
    }
    texts.update(overrides or {})
    return {"drafts": [{"field": k, "options": [{"text": t, "addresses": ["F1", "F99"]} for t in v]}
                       for k, v in texts.items()]}


FINDINGS = [{"id": 1, "severity": "high", "title": "Headline lists methods", "fix": "Lead with the role"}]


def test_fields_follow_the_sources_you_have():
    keys = [f["key"] for f in pd_.fields_for(_snaps())]
    assert keys == ["linkedin:export#headline", "linkedin:export#about", "github:me#bio",
                    "github:me#readme", "site:https://me.dev/#tagline"]
    site = pd_.fields_for(_snaps())[-1]
    assert site["label"] == "Site tagline (https://me.dev/)" and site["current"] == "Measurement lead"


def test_claim_check_flags_invented_numbers_and_names():
    ref = RESUME + " Applied AI & ML | MMM"
    assert pd_.unsupported("Sr. Director | MMM at Globex, $40M", ref) == []
    assert pd_.unsupported("Led a team of 5 managing $60M across Acme and Initech", ref) == ["60", "Acme"]
    # form is forgiven, substance is not
    assert pd_.unsupported("Prev. Globex. Ex-Globex. Initech's team. Today I lead.", ref) == []
    assert pd_.unsupported("I lead teams.\n\nToday I run MMM.", ref) == []            # paragraph start
    assert pd_.unsupported("Today I work with Today Corp.", ref) == ["Today"]         # a name after all
    assert pd_.unsupported("Ex-Hooli lead. Worked with Umbrella.", ref) == ["Ex-Hooli", "Umbrella"]


def test_drafts_are_checked_and_over_length_options_retried_once():
    llm = StubLLM({"profile-drafts": [_answer(), {"github:me#bio|1": "Short enough now."}]})
    drafts = pd_.make_drafts(llm, _brief(), _snaps(), FINDINGS, RESUME, log=lambda *_: None)
    by_key = {d["key"]: d for d in drafts}
    head = by_key["linkedin:export#headline"]["options"]
    assert head[0]["needs_check"] == [] and head[1]["needs_check"] == ["60", "Acme"]
    assert head[0]["addresses"] == [1]                    # unknown finding refs dropped
    bio = by_key["github:me#bio"]["options"][1]
    assert bio["text"] == "Short enough now." and not bio["over"]
    assert [t for t, _ in llm.calls] == ["profile-drafts", "profile-drafts"]


def test_still_over_after_the_retry_is_flagged():
    llm = StubLLM({"profile-drafts": [_answer(), {"github:me#bio|1": "y" * 170}]})
    drafts = pd_.make_drafts(llm, _brief(), _snaps(), FINDINGS, RESUME, log=lambda *_: None)
    bio = next(d for d in drafts if d["key"] == "github:me#bio")["options"][1]
    assert bio["over"] is True and bio["chars"] == 170


def _brief():
    return {"asks": [{"ask": "mmm", "roles": 3, "share": 0.6}], "with_jd": 5, "functions": [],
            "stated_keywords": [], "watch_topics": [], "claims": [],
            "persona": {"source": "Persona", "text": "dry, specific"}}


# ---------- files and approval ----------

def test_draft_file_round_trips_and_approve_keeps_the_previous_copy(tmp_path):
    (tmp_path / "me").mkdir()
    llm = StubLLM({"profile-drafts": [_answer(), {"github:me#bio|1": "Short."}]})
    drafts = pd_.make_drafts(llm, _brief(), _snaps(), FINDINGS, RESUME, log=lambda *_: None)
    text = pd_.render_draft_file(drafts, 7)
    assert "## linkedin:export#headline" in text and "<!-- option 2" in text
    assert "needs check: 60, Acme" in text
    parsed = pd_.parse_persona(text)
    assert parsed["linkedin:export#headline"] == "Sr. Director, Marketing Science | MMM at Globex"
    assert "Acme" not in parsed["linkedin:export#headline"]        # option 2 is only a comment

    (tmp_path / pd_.DRAFT).write_text(text)
    (tmp_path / pd_.PERSONA).write_text("## github:me#bio\nOld bio\n")
    pd_.approve_draft(tmp_path)
    assert pd_.load_approved(tmp_path)["github:me#bio"] == "Measurement leader. MMM, causal inference."
    assert "Old bio" in (tmp_path / pd_.PREVIOUS).read_text()


def test_approve_field_sets_one_field_only(tmp_path):
    (tmp_path / "me").mkdir()
    pd_.approve_field(tmp_path, "github:me#bio", "One")
    old, new = pd_.approve_field(tmp_path, "linkedin:export#headline", "Two")
    assert old is None and new == "Two"
    assert pd_.load_approved(tmp_path) == {"github:me#bio": "One", "linkedin:export#headline": "Two"}


def test_approved_but_not_published(tmp_path):
    (tmp_path / "me").mkdir()
    snaps = _snaps()
    pd_.approve_field(tmp_path, "github:me#bio", "Measurement leader. MMM, causal inference.")
    pd_.approve_field(tmp_path, "site:https://me.dev/#tagline", "Measurement lead")   # already live
    found = pd_.unpublished_findings(tmp_path, snaps)
    assert [f["title"] for f in found] == ["Approved GitHub bio is not on the profile yet"]
    assert found[0]["source_key"] == "github:me" and found[0]["severity"] == "low"

    snaps[1]["identity"]["bio"] = "Measurement leader. MMM, causal inference."
    assert pd_.unpublished_findings(tmp_path, snaps) == []


# ---------- storage, page, dashboard ----------

def _stored(base):
    (base / "me").mkdir(exist_ok=True)
    (base / "artifacts/html").mkdir(parents=True, exist_ok=True)
    (base / "me/resume.txt").write_text(RESUME)
    store = Store(base)
    for s in _snaps():
        store.save_snapshot_if_changed(s)
    snaps = store.latest_snapshots()
    eid = store.add_evaluation({
        "created_at": "2026-09-27T12:00:00", "trigger": "forced",
        "snapshot_ids": {s["source_key"]: s["_id"] for s in snaps}, "brief_hash": "b",
        "brief": {**_brief(), "pursued": 5, "window_days": 60},
        "scores": {"overall": {"coverage": 3}, "per_source": {s["source_key"]: {} for s in snaps}},
        "shares": {}, "per_source": [], "summary": "ok"})
    return store, eid


def test_draft_for_latest_stores_drafts_and_writes_the_file(tmp_path):
    store, eid = _stored(tmp_path)
    llm = StubLLM({"profile-drafts": [_answer(), {"github:me#bio|1": "Short."}]})
    path = pd_.draft_for_latest(tmp_path, llm=llm, log=lambda *_: None)
    assert path == tmp_path / pd_.DRAFT and path.exists()
    assert len(store.latest_evaluation()["drafts"]) == 5


def test_page_shows_drafts_with_flags_and_approved_text(tmp_path):
    store, _ = _stored(tmp_path)
    pd_.draft_for_latest(tmp_path, llm=StubLLM({"profile-drafts": [_answer(), {"github:me#bio|1": "S."}]}),
                         log=lambda *_: None)
    pd_.approve_field(tmp_path, "github:me#bio", "Approved bio text")
    html = build.render_profile(build._env(), write=False, base=tmp_path)[1]
    assert "Replacement text, for you to approve" in html
    assert "needs check: Acme" in html
    assert "<b>Approved</b>Approved bio text" in html
    assert 'data-key="linkedin:export#headline" data-limit="220"' in html
    assert "body.live .approvebox{display:flex}" in html


@pytest.fixture
def server(tmp_path):
    import dashboard as srv
    store, _ = _stored(tmp_path)
    (tmp_path / "artifacts/html/index.html").write_text("<html></html>")
    pd_.draft_for_latest(tmp_path, llm=StubLLM({"profile-drafts": [_answer(), {"github:me#bio|1": "S."}]}),
                         log=lambda *_: None)
    httpd = srv.serve(port=8795, base=tmp_path, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield "http://127.0.0.1:8795", store, tmp_path
    httpd.shutdown()
    httpd.server_close()


def test_approval_route_writes_one_field_and_logs_it(server):
    api, store, base = server
    r = requests.post(f"{api}/api/persona", json={"field": "github:me#bio", "text": "My new bio"}, timeout=5)
    assert r.status_code == 200 and r.json() == {"ok": True, "field": "github:me#bio", "changed": True}
    assert pd_.load_approved(base) == {"github:me#bio": "My new bio"}
    assert any(c["field"] == "persona:github:me#bio" for c in store.history(limit=5))


@pytest.mark.parametrize("body", [
    {"field": "github:me#bio", "text": "x" * 161},
    {"field": "github:me#bio", "text": "   "},
    {"field": "github:me#nickname", "text": "hi"},
    {"field": "github:me#bio", "text": 5},
])
def test_approval_route_rejects_bad_requests(server, body):
    api, _, base = server
    r = requests.post(f"{api}/api/persona", json=body, timeout=5)
    assert r.status_code == 400 and "error" in r.json()
    assert not (base / pd_.PERSONA).exists()


def test_approval_route_refuses_other_machines(server, monkeypatch):
    import dashboard as srv
    api, _, base = server
    monkeypatch.setattr(srv.Handler, "_loopback", lambda self: False)
    r = requests.post(f"{api}/api/persona", json={"field": "github:me#bio", "text": "x"}, timeout=5)
    assert r.status_code == 403 and not (base / pd_.PERSONA).exists()

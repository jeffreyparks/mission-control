"""Profile review guidance: which dismissals become candidates, that counts come
from the evidence and not the model, when a draft is due, and that only an
approved me/profile-guidance.md ever reaches the evaluator. Every finding and
reason here is made up."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO))

import profile_guidance as pg  # noqa: E402
from context import guidance_block  # noqa: E402
from store import Store  # noqa: E402


class StubLLM:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def complete_json(self, prompt, tag="generic", validate=None, **_):
        self.calls.append((tag, prompt))
        data = self.answer(prompt) if callable(self.answer) else self.answer
        if validate:
            assert validate(data), "stub answer fails validation"
        return data


def _finding(conn, n, dimension, source, state, ask=None, note=None, title=None):
    conn.execute(
        "INSERT INTO profile_findings(fingerprint, evaluation_id, first_seen, dimension, source_key, "
        "severity, title, ask, fix, state, dismiss_note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"fp{n}", 1, "2026-09-20", dimension, source, "medium", title or f"Finding {n}", ask,
         "Do something", state, note))


@pytest.fixture
def base(tmp_path):
    (tmp_path / "me").mkdir()
    store = Store(tmp_path)
    with store.connect() as conn:
        # Register on GitHub: dismissed four times, never upheld - a pattern.
        for n in range(4):
            _finding(conn, n, "register", "github:octo-example", "dismissed")
        # One dismissal with a reason that generalises.
        _finding(conn, 10, "register", "github:octo-example", "dismissed",
                 note="GitHub is a code portfolio, not a voice", title="Terse READMEs")
        # Coverage of 'sql' on the site: dismissed three times but upheld three
        # times - under the share bar, so no pattern.
        for n in range(20, 23):
            _finding(conn, n, "coverage", "site:https://example.dev/", "dismissed", ask="sql")
        for n in range(23, 26):
            _finding(conn, n, "coverage", "site:https://example.dev/", "fixed", ask="sql")
        # Two dismissals without reasons elsewhere: too few to say anything.
        for n in range(30, 32):
            _finding(conn, n, "proof", "bluesky:example.bsky.social", "dismissed")
        # Still open: not a judgment, never evidence.
        _finding(conn, 40, "drift", "linkedin", "open")
    return tmp_path


def test_candidates_are_reasons_and_clear_patterns(base):
    ev = pg.build_evidence(pg.load_findings(Store(base)))
    assert ev["dismissed"] == 10 and ev["with_reason"] == 1 and ev["upheld"] == 3
    patterns = {c["value"] for c in ev["candidates"] if c["kind"] == "pattern"}
    assert "register on github:octo-example" in patterns
    assert "register" in patterns
    assert not any("sql" in v or "proof" in v for v in patterns)
    reasons = [c for c in ev["candidates"] if c["kind"] == "reason"]
    assert [r["reason"] for r in reasons] == ["GitHub is a code portfolio, not a voice"]
    assert reasons[0]["upheld_same_dimension_and_source"] == 0
    github = next(c for c in ev["candidates"] if c["value"] == "register on github:octo-example")
    assert github["dismissed"] == 5 and github["reasons"] == ["GitHub is a code portfolio, not a voice"]
    assert [c["id"] for c in ev["candidates"]] == [f"c{i}" for i in range(1, len(ev["candidates"]) + 1)]


def test_draft_attaches_counts_from_evidence_not_the_model(base):
    def answer(prompt):
        ev = pg.build_evidence(pg.load_findings(Store(base)))
        out = []
        for c in ev["candidates"]:
            if c["kind"] == "reason":
                out.append({"id": c["id"], "keep": True,
                            "statement": "On GitHub, don't judge register against the persona."})
            else:
                out.append({"id": c["id"], "keep": False, "why_dropped": "covered by the reason"})
        return out

    llm = StubLLM(answer)
    path = pg.write_draft(base, llm=llm)
    text = path.read_text()
    assert "- On GitHub, don't judge register against the persona." in text
    assert "because: GitHub is a code portfolio, not a voice" in text
    assert "covered by the reason" in text
    assert llm.calls[0][0] == "profile-guidance"
    assert "GitHub is a code portfolio" in llm.calls[0][1]


def test_validator_rejects_unknown_ids_and_empty_statements(base):
    ev = pg.build_evidence(pg.load_findings(Store(base)))
    validate = pg.make_validator(ev)
    assert not validate([])
    assert not validate([{"id": "c999", "keep": False}])
    assert not validate([{"id": "c1", "keep": True, "statement": " "}])
    assert validate([{"id": "c1", "keep": False, "why_dropped": "one-off"}])


def test_no_candidates_writes_an_empty_draft_without_a_call(tmp_path):
    (tmp_path / "me").mkdir()
    Store(tmp_path)
    llm = StubLLM(lambda _p: pytest.fail("no model call expected"))
    text = pg.write_draft(tmp_path, llm=llm).read_text()
    assert "No standing rule" in text and llm.calls == []


def test_draft_is_due_only_when_dismissals_change(base):
    assert pg.draft_is_due(base) == (True, "no draft yet")
    pg.write_draft(base, llm=StubLLM(lambda p: [{"id": "c1", "keep": False, "why_dropped": "x"}]))
    assert pg.draft_is_due(base)[0] is False

    store = Store(base)
    fid = pg.load_findings(store)[0]["id"]
    store.set_finding_state(fid, "dismissed", note="Pinned repos are deliberate")
    assert pg.draft_is_due(base) == (True, "dismissals changed")


def test_nothing_dismissed_is_not_due(tmp_path):
    (tmp_path / "me").mkdir()
    assert pg.draft_is_due(tmp_path) == (False, "no database yet")
    Store(tmp_path)
    assert pg.draft_is_due(tmp_path) == (False, "nothing dismissed yet")


def test_only_the_approved_file_reaches_the_reviewer(base):
    pg.write_draft(base, llm=StubLLM(lambda p: [
        {"id": c["id"], "keep": c["kind"] == "reason",
         "statement": "Don't judge GitHub register.", "why_dropped": "dup"}
        for c in pg.build_evidence(pg.load_findings(Store(base)))["candidates"]]))
    assert guidance_block(base) == ""

    pg.approve(base)
    block = guidance_block(base)
    assert "Don't judge GitHub register." in block
    assert "<!--" not in block and "evidence-hash" not in block

    (base / pg.APPROVED).write_text("# Profile review guidance\n\n- Older rule.\n")
    pg.approve(base)
    assert "Older rule." in (base / pg.PREVIOUS).read_text()

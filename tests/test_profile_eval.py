"""Profile brief and evaluation: role selection, ask counting, persona, quote
checking, drift, freshness, triggers, the findings lifecycle and the report.
Every model call is stubbed."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO))

import profile_brief  # noqa: E402
import profile_eval  # noqa: E402
from profile_snapshot import CONFIG_DEFAULTS, make_snapshot  # noqa: E402
from store import Store  # noqa: E402

NOW = datetime(2026, 9, 27, 12, 0, 0)
CONFIG = dict(CONFIG_DEFAULTS)


class StubLLM:
    """Answers complete_json by tag; records every prompt."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []
        self.stats = {"misses": 0}

    def complete_json(self, prompt, tag="generic", validate=None, **_):
        self.calls.append((tag, prompt))
        self.stats["misses"] += 1
        answer = self.answers[tag]
        data = answer(prompt) if callable(answer) else answer
        if validate:
            assert validate(data), f"stub answer for {tag} fails validation"
        return data

    def report(self):
        return f"LLM: {self.stats['misses']} calls"


# ---------- fixtures ----------

def _role(conn, rid, status="01 Open", fit=None, outcomes=None, close_reason=None,
          opened="2026-09-10", applied=None, function="Data Science & ML"):
    conn.execute(
        "INSERT INTO roles(id, org, title, status, outcomes, close_reason, fit_score, "
        "date_opened, date_applied, role_function) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, f"Org {rid}", f"Title {rid}", status, outcomes, close_reason, fit, opened, applied, function))


@pytest.fixture
def base(tmp_path):
    (tmp_path / "me").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "me/profile.md").write_text(
        "# Profile\n\n## Target Keywords\n- experiment design\n- causal inference\n\n"
        "## Watch Topics\n- ai evaluation: where the field is going\n\n"
        "## Career Positioning\nA measurement leader who ships causal systems.\n\n"
        "## Notes\nnothing\n")
    (tmp_path / "me/resume.txt").write_text("Led a 5-person team. Built an MMM platform.")
    store = Store(tmp_path)
    with store.connect() as conn:
        _role(conn, "applied", status="03 Applied", fit=40, applied="2026-09-12")
        _role(conn, "researching", status="02 Researching", fit=30)
        _role(conn, "heard-back", status="04 Closed", outcomes="Rejected after screen", fit=20)
        _role(conn, "high-fit", fit=78)
        _role(conn, "high-fit-rejected", status="04 Closed", fit=80,
              close_reason="Not a fit - function")
        _role(conn, "low-fit", fit=30)
        _role(conn, "too-old", status="03 Applied", fit=70, opened="2026-05-01", applied="2026-05-02")
        _role(conn, "high-fit-2", fit=72, function="Marketing")
    jds = {rid: {"jd": f"Description for {rid}"} for rid in
           ("applied", "researching", "heard-back", "high-fit", "high-fit-2", "low-fit")}
    (tmp_path / "data/job-descriptions.json").write_text(json.dumps(jds))
    return tmp_path


ASKS = {
    "Title applied": ["Experiment Design", "SQL", "causal-inference"],
    "Title researching": ["experiment design", "python", "sql"],
    "Title heard-back": ["experiment design", "sql", "sql"],
    "Title high-fit": ["marketing mix modeling", "experiment design"],
    "Title high-fit-2": ["python"],
}


def _asks_answer(prompt):
    out = {}
    for line in prompt.splitlines():
        if line.startswith("[r"):
            key, rest = line[1:].split("] ", 1)
            title = rest.split(" at ")[0]
            out[key] = ASKS[title]
    return out


def _identity_merge(prompt):
    return {}


def _stub(**extra):
    answers = {"profile-asks": _asks_answer, "profile-ask-merge": _identity_merge,
               "profile-claims": [{"claim": "Led a 5-person team"}, {"claim": "Built an MMM platform"}]}
    answers.update(extra)
    return StubLLM(answers)


# ---------- the brief ----------

def test_pursued_roles_follow_actions_fit_and_window(base):
    roles = profile_brief.pursued_roles(base, CONFIG, now=NOW)
    ids = {r["id"] for r in roles}
    assert ids == {"applied", "researching", "heard-back", "high-fit", "high-fit-2"}
    # closed for a reason about the role: out, whatever its score; older than 60 days: out


def test_asks_are_counted_once_per_role_by_python(base):
    brief = profile_brief.build_brief(base, CONFIG, llm=_stub(), now=NOW)
    counts = {a["ask"]: a["roles"] for a in brief["asks"]}
    assert counts["experiment design"] == 4          # case and hyphens folded
    assert counts["sql"] == 3                        # "sql" twice in one role counts once
    assert counts["causal inference"] == 1
    assert brief["asks"][0]["ask"] == "experiment design"
    assert brief["with_jd"] == 5 and brief["pursued"] == 5 and not brief["fallback"]
    assert brief["asks"][0]["share"] == 0.8
    assert brief["functions"][0] == {"function": "Data Science & ML", "roles": 4}
    assert brief["claims"] == ["Led a 5-person team", "Built an MMM platform"]
    assert brief["watch_topics"] == ["ai evaluation"]


def test_synonyms_are_merged_before_counting(base):
    def merge(prompt):
        # a (deliberately bad) merge, to see it applied
        return {"causal inference": "experiment design", "python": "sql"}

    brief = profile_brief.build_brief(base, CONFIG, llm=_stub(**{"profile-ask-merge": merge}), now=NOW)
    counts = {a["ask"]: a["roles"] for a in brief["asks"]}
    assert "causal inference" not in counts and "python" not in counts
    assert counts["experiment design"] == 4       # 'applied' had both: still counted once
    assert counts["sql"] == 4                     # sql in 3 roles + python-only high-fit-2


def test_merge_must_map_into_the_list():
    validate = profile_brief._merge_validator(["a", "b"])
    assert validate({"b": "a"}) and validate({})
    assert not validate({"b": "c"})                 # canonical not in the list
    assert not validate({"z": "a"})                 # a term that was never listed


def test_a_failed_merge_counts_unmerged(base):
    from llm import TruncatedResponse

    def boom(prompt):
        raise TruncatedResponse("cut off")

    logs = []
    llm = _stub(**{"profile-ask-merge": boom})
    merged = profile_brief.merge_synonyms(llm, {"r": ["a", "b"]}, log=logs.append)
    assert merged == {"r": ["a", "b"]} and "counting unmerged" in logs[0]


def test_too_few_descriptions_fall_back_to_stated_keywords(base):
    (base / "data/job-descriptions.json").write_text(json.dumps({"applied": {"jd": "x"}}))
    llm = _stub()
    brief = profile_brief.build_brief(base, CONFIG, llm=llm, now=NOW)
    assert brief["fallback"] is True
    assert [a["ask"] for a in brief["asks"]] == ["experiment design", "causal inference"]
    assert all(a["roles"] is None for a in brief["asks"])
    assert not any(tag == "profile-asks" for tag, _ in llm.calls)


def test_persona_is_career_positioning_else_default(base):
    persona = profile_brief.load_persona(base)
    assert persona == {"source": "Career Positioning",
                       "text": "A measurement leader who ships causal systems."}
    (base / "me/profile.md").write_text("# Profile\n")
    assert profile_brief.load_persona(base)["source"] == "default"


# ---------- per-source helpers ----------

def _site(text="I design experiments and causal inference studies.", items=None):
    items = items if items is not None else [
        {"kind": "section", "id": "s1", "title": "About", "text": text, "url": "https://me.dev/"},
        {"kind": "section", "id": "s2", "title": "Hobbies", "text": "Sourdough and cycling.",
         "url": "https://me.dev/"},
    ]
    snap = make_snapshot("site", "site:https://me.dev/", {"hero": "Measurement lead"}, items)
    snap["_id"] = 1
    return snap


def _brief():
    return {"asks": [{"ask": "experiment design", "roles": 4, "share": 0.8},
                     {"ask": "causal inference", "roles": 2, "share": 0.4},
                     {"ask": "sql", "roles": 3, "share": 0.6}],
            "with_jd": 5, "pursued": 5, "window_days": 60, "functions": [],
            "stated_keywords": [], "watch_topics": [], "claims": [],
            "persona": {"source": "default", "text": "neutral"}, "learned": "", "hash": "h"}


def test_coverage_matrix_is_exact_and_per_source():
    github = make_snapshot("github", "github:me", {"bio": "SQL and causal-inference"}, [])
    matrix = profile_eval.coverage_matrix(_brief(), [_site(), github])
    assert matrix["causal inference"] == {"site:https://me.dev/": True, "github:me": True}
    assert matrix["sql"] == {"site:https://me.dev/": False, "github:me": True}
    assert matrix["experiment design"]["site:https://me.dev/"] is False  # "design experiments"


def test_quotes_may_carry_a_copied_field_label():
    text = profile_eval._norm("Initech / Globex\nSpringfield")
    assert profile_eval.quote_in("company: Initech / Globex", text)
    assert profile_eval.quote_in("Initech / Globex", text)
    assert not profile_eval.quote_in("company: Google", text)


def test_drift_shares_weight_reposts_at_half():
    items = [{"kind": "post", "id": "a", "text": "x"},
             {"kind": "post", "id": "b", "text": "y"},
             {"kind": "repost", "id": "c", "text": "z", "weight": 0.5}]
    snap = make_snapshot("bluesky", "bluesky:me", {}, items)
    shares = profile_eval.drift_shares(snap, {"a": "on", "b": "tangent", "c": "tangent"})
    assert shares == {"on": 0.4, "adjacent": 0.0, "tangent": 0.6}


@pytest.mark.parametrize("days, score", [(3, 5), (60, 4), (120, 2), (400, 1), (None, None)])
def test_freshness_rule(days, score):
    snap = make_snapshot("github", "github:me", {}, [], {"days_since_activity": days})
    assert profile_eval.freshness_score(snap) == score


def _source_answer(findings):
    return {"scores": {"coverage": 3, "drift": 4, "proof": 2, "register": 4},
            "score_notes": {}, "item_tags": {"i1": "on", "i2": "tangent"}, "findings": findings}


def test_findings_with_invented_quotes_or_false_absences_are_dropped():
    snap = _site()
    brief = _brief()
    coverage = profile_eval.coverage_matrix(brief, [snap])
    findings = [
        {"dimension": "register", "severity": "medium", "title": "Real quote",
         "quote": "I design  experiments", "item": "i1", "ask": None, "fix": "Tighten it"},
        {"dimension": "register", "severity": "high", "title": "Invented quote",
         "quote": "I am a thought leader", "item": None, "ask": None, "fix": "x"},
        {"dimension": "coverage", "severity": "high", "title": "No SQL", "quote": None,
         "item": None, "ask": "SQL", "fix": "Mention SQL work"},
        {"dimension": "coverage", "severity": "high", "title": "No causal inference", "quote": None,
         "item": None, "ask": "causal inference", "fix": "x"},
        {"dimension": "drift", "severity": "low", "title": "Absence with no ask", "quote": None,
         "item": None, "ask": None, "fix": "x"},
    ]
    logs = []
    result = profile_eval.judge_source(_stub(**{"profile-eval-source": _source_answer(findings)}),
                                       brief, snap, coverage, log=logs.append)
    assert [f["title"] for f in result["findings"]] == ["Real quote", "No SQL"]
    assert result["findings"][0]["url"] == "https://me.dev/"
    assert result["findings"][1]["ask"] == "sql" and result["findings"][1]["ask_roles"] == 3
    assert len(logs) == 3
    assert result["shares"] == {"on": 0.5, "adjacent": 0.0, "tangent": 0.5}


# ---------- synthesis, storage, lifecycle ----------

def _synth_answer(keep, consistency=()):
    return {"overall": {"coverage": 3, "drift": 4, "consistency": 2, "proof": 2, "register": 4},
            "summary": "Mostly on target. Name SQL.", "keep": list(keep),
            "consistency_findings": list(consistency)}


def _evaluate(base, findings, keep, consistency=(), force=False, now=NOW):
    llm = _stub(**{"profile-eval-source": _source_answer(findings),
                   "profile-eval-synth": _synth_answer(keep, consistency)})
    return profile_eval.evaluate(base, force=force, llm=llm, log=lambda *_: None, now=now), llm


SQL_GAP = {"dimension": "coverage", "severity": "high", "title": "No SQL", "quote": None,
           "item": None, "ask": "sql", "fix": "Mention SQL work"}
TONE = {"dimension": "register", "severity": "low", "title": "Hobby section",
        "quote": "Sourdough and cycling.", "item": "i2", "ask": None, "fix": "Cut it"}


def test_evaluate_stores_scores_findings_and_consistency(base):
    store = Store(base)
    store.save_snapshot_if_changed(_site())
    consistency = [
        {"source_key": "site:https://me.dev/", "severity": "medium", "title": "Hero vague",
         "quote": "Measurement lead", "fix": "Say what kind"},
        {"source_key": "site:https://me.dev/", "severity": "high", "title": "Made up",
         "quote": "Chief Data Officer", "fix": "x"},
    ]
    (evaluation_id, why), llm = _evaluate(base, [SQL_GAP, TONE], keep=["f1", "f2"],
                                          consistency=consistency)
    assert evaluation_id and why == "first evaluation"
    ev = store.latest_evaluation()
    assert ev["scores"]["overall"]["consistency"] == 2
    assert ev["scores"]["overall"]["freshness"] is None           # a site has no dates
    assert ev["summary"] == "Mostly on target. Name SQL."
    titles = [f["title"] for f in store.profile_findings()]
    assert titles == ["No SQL", "Hero vague", "Hobby section"]     # severity, then role count
    tags = [t for t, _ in llm.calls]
    assert tags.count("profile-eval-source") == 1 and tags.count("profile-eval-synth") == 1
    for tag, prompt in llm.calls:
        if tag.startswith("profile-eval"):
            assert "Today is 2026-09-27." in prompt


def test_triggers(base):
    store = Store(base)
    store.save_snapshot_if_changed(_site())
    _evaluate(base, [SQL_GAP], keep=["f1"])

    (eid, why), _ = _evaluate(base, [SQL_GAP], keep=["f1"], now=NOW + timedelta(days=2))
    assert eid is None and why.startswith("nothing changed")

    (eid, why), _ = _evaluate(base, [SQL_GAP], keep=["f1"], now=NOW + timedelta(days=8))
    assert eid and "days since" in why

    (eid, why), _ = _evaluate(base, [SQL_GAP], keep=["f1"], force=True, now=NOW + timedelta(days=8))
    assert eid and why == "forced"

    edited = _site(text="Now I also write SQL.")
    store.save_snapshot_if_changed(edited)
    (eid, why), _ = _evaluate(base, [TONE], keep=["f1"], now=NOW + timedelta(days=9))
    assert eid and why == "changed: site:https://me.dev/"


def test_top_asks_change_triggers(base):
    store = Store(base)
    store.save_snapshot_if_changed(_site())
    _evaluate(base, [SQL_GAP], keep=["f1"])
    ASKS["Title high-fit-2"] = ["python", "dbt", "airflow", "looker", "tableau", "spark"]
    try:
        (eid, why), _ = _evaluate(base, [SQL_GAP], keep=["f1"], now=NOW + timedelta(days=1))
    finally:
        ASKS["Title high-fit-2"] = ["python"]
    assert eid and why == "the brief's top asks changed"


def test_findings_lifecycle(base):
    store = Store(base)
    store.save_snapshot_if_changed(_site())
    _evaluate(base, [SQL_GAP, TONE], keep=["f1", "f2"], force=True)
    by_title = {f["title"]: f for f in store.profile_findings()}
    with store.connect() as conn:
        conn.execute("UPDATE profile_findings SET state='dismissed' WHERE id=?",
                     (by_title["Hobby section"]["id"],))

    # both raised again, reworded: dismissed stays dismissed; open stays open
    sql_reworded = {**SQL_GAP, "title": "SQL is nowhere on the site"}
    _evaluate(base, [sql_reworded, TONE], keep=["f1", "f2"], force=True)
    open_now = store.profile_findings()
    assert [f["title"] for f in open_now] == ["SQL is nowhere on the site"]

    # SQL gap not raised, site unchanged: the reviewer just skipped it - held open
    (_, _), _ = _evaluate(base, [TONE], keep=["f1"], force=True)
    assert [f["title"] for f in store.profile_findings()] == ["SQL is nowhere on the site"]

    # SQL gap not raised after the site changed: fixed
    store.save_snapshot_if_changed(_site(text="I design experiments and causal studies."))
    _evaluate(base, [TONE], keep=["f1"], force=True)
    assert store.profile_findings() == []
    fixed = store.profile_findings(states=("fixed",))
    assert [f["title"] for f in fixed] == ["SQL is nowhere on the site"]
    assert fixed[0]["state_note"].startswith("resolved by evaluation")

    # raised again: reopens, marked came back
    _evaluate(base, [SQL_GAP, TONE], keep=["f1", "f2"], force=True)
    back = store.profile_findings()
    assert [f["title"] for f in back] == ["No SQL"] and back[0]["came_back"] == 1

    # a dismissed finding whose severity rises reopens
    _evaluate(base, [SQL_GAP, {**TONE, "severity": "high"}], keep=["f1", "f2"], force=True)
    assert {f["title"] for f in store.profile_findings()} == {"No SQL", "Hobby section"}


def test_fixed_needs_a_change_to_the_findings_own_source():
    from store import _source_changed
    raised = {"site:a": 1, "github:b": 5}
    assert not _source_changed("site:a", raised, {"site:a": 1, "github:b": 6})   # other source moved
    assert _source_changed("site:a", raised, {"site:a": 2, "github:b": 5})
    assert not _source_changed("site:a", raised, {"github:b": 6})                # site not judged
    assert _source_changed("site:a", {}, {"site:a": 1})                          # no record: old rule
    assert _source_changed(None, raised, {"site:a": 1, "github:b": 6})           # all-sources finding
    assert not _source_changed(None, raised, {"site:a": 1, "github:b": 5})


def test_dismiss_reason_survives_reopening(base):
    store = Store(base)
    store.save_snapshot_if_changed(_site())
    _evaluate(base, [SQL_GAP, TONE], keep=["f1", "f2"], force=True)
    tone = next(f for f in store.profile_findings() if f["title"] == "Hobby section")
    store.set_finding_state(tone["id"], "dismissed", note="Personal touch is on purpose")

    _evaluate(base, [SQL_GAP, {**TONE, "severity": "high"}], keep=["f1", "f2"], force=True)
    back = next(f for f in store.profile_findings() if f["title"] == "Hobby section")
    assert back["state_note"].startswith("reopened")
    assert back["dismiss_note"] == "Personal touch is on purpose"

    store.set_finding_state(tone["id"], "dismissed")       # no new reason: keeps the old one
    again = store.profile_findings(states=("dismissed",))[0]
    assert again["dismiss_note"] == "Personal touch is on purpose"


def test_dismiss_reasons_are_backfilled_from_the_change_log(base):
    store = Store(base)
    store.save_snapshot_if_changed(_site())
    _evaluate(base, [SQL_GAP, TONE], keep=["f1", "f2"], force=True)
    tone = next(f for f in store.profile_findings() if f["title"] == "Hobby section")
    store.set_finding_state(tone["id"], "dismissed", note="Keep it (deliberately)")
    with store.connect() as conn:                          # a database from before the column
        conn.execute("ALTER TABLE profile_findings DROP COLUMN dismiss_note")
    Store(base)
    row = store.profile_findings(states=("dismissed",))[0]
    assert row["dismiss_note"] == "Keep it (deliberately)"


def test_approved_guidance_reaches_both_review_prompts(base):
    store = Store(base)
    store.save_snapshot_if_changed(_site())
    (base / "me/profile-guidance.draft.md").write_text("- Never flag hobbies on the site.\n")
    _, llm = _evaluate(base, [SQL_GAP], keep=["f1"], force=True)
    assert not any("REVIEW GUIDANCE" in p for _, p in llm.calls)   # a draft is not read

    (base / "me/profile-guidance.md").write_text(
        "# Profile review guidance\n\n- Never flag hobbies on the site.\n  <!-- evidence: x -->\n")
    _, llm = _evaluate(base, [SQL_GAP], keep=["f1"], force=True)
    prompts = {tag: p for tag, p in llm.calls if tag.startswith("profile-eval")}
    for tag in ("profile-eval-source", "profile-eval-synth"):
        assert "REVIEW GUIDANCE" in prompts[tag]
        assert "- Never flag hobbies on the site." in prompts[tag]
        assert "evidence: x" not in prompts[tag]


def test_report(base):
    store = Store(base)
    store.save_snapshot_if_changed(_site())
    _evaluate(base, [SQL_GAP, TONE], keep=["f1", "f2"])
    text = profile_eval.render_report(store)
    assert text.startswith("# Profile evaluation - 2026-09-27")
    assert "| consistency | 2 |" in text
    assert "- **[high] No SQL** (coverage, site:https://me.dev/)" in text
    assert "Ask: sql (3 roles)" in text
    assert "> Sourdough and cycling." in text
    assert "site:https://me.dev/: 50% on target, 0% adjacent, 50% tangent" in text


def test_no_snapshots_means_no_evaluation(base):
    (eid, why), llm = _evaluate(base, [], keep=[])
    assert eid is None and "no profile snapshots" in why and llm.calls == []


def test_evaluation_raises_approved_but_not_published(base):
    import profile_drafts
    store = Store(base)
    store.save_snapshot_if_changed(_site())
    profile_drafts.approve_field(base, "site:https://me.dev/#tagline", "A tagline not yet on the site")
    _evaluate(base, [SQL_GAP], keep=["f1"])
    titles = [f["title"] for f in store.profile_findings()]
    assert "Approved Site tagline (https://me.dev/) is not on the profile yet" in titles

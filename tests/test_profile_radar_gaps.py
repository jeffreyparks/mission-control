"""Profile gaps feed the Content Radar: which findings become gap topics, the
prompt block, pick labelling, and the radar page. No model calls."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "render"))

import content_radar  # noqa: E402
from profile_eval import fingerprint, profile_gap_topics  # noqa: E402
from store import Store  # noqa: E402


def _finding(ask, severity="high", dimension="coverage", source="linkedin:export", roles=5):
    f = {"dimension": dimension, "severity": severity, "title": f"No {ask}", "quote": None,
         "url": None, "ask": ask, "ask_roles": roles, "fix": "x", "source_key": source}
    f["fingerprint"] = fingerprint(f)
    return f


@pytest.fixture
def base(tmp_path):
    store = Store(tmp_path)
    eid = store.add_evaluation({
        "created_at": "2026-09-27T12:00:00", "trigger": "forced", "snapshot_ids": {},
        "brief_hash": "b", "brief": {"with_jd": 15}, "scores": {"overall": {}}})
    findings = [
        _finding("a/b testing", roles=9),
        _finding("a/b testing", roles=9, source="site:https://me.dev/"),
        _finding("team leadership", severity="medium", roles=8),
        _finding("sql", severity="low", roles=2),                      # too minor
        _finding("attribution", dimension="proof", roles=3),           # not a coverage gap
        _finding("causal inference", roles=4),
        _finding("stakeholder management", severity="medium", roles=4, source="github:me"),
        _finding("measurement frameworks", roles=5),
        _finding("forecasting", severity="medium", roles=1),
    ]
    store.sync_findings(eid, findings)
    return tmp_path


def test_gap_topics_are_coverage_gaps_ranked_and_capped(base):
    gaps = profile_gap_topics(base)
    assert [g["topic"] for g in gaps] == ["a/b testing", "measurement frameworks", "causal inference",
                                          "team leadership", "stakeholder management"]
    assert gaps[0]["note"] == "9 of 15 pursued roles ask for it; not shown on LinkedIn, your site me.dev"


def test_a_dismissed_gap_drops_out(base):
    store = Store(base)
    fid = next(f["id"] for f in store.profile_findings() if f["ask"] == "measurement frameworks")
    store.set_finding_state(fid, "dismissed")
    topics = [g["topic"] for g in profile_gap_topics(base)]
    assert "measurement frameworks" not in topics and "forecasting" in topics


def test_no_database_means_no_gaps_and_no_database_created(tmp_path):
    assert profile_gap_topics(tmp_path) == []
    assert not (tmp_path / "data").exists()


def _radar(base, watch=(), gaps=None):
    radar = content_radar.ContentRadar.__new__(content_radar.ContentRadar)
    radar.base_dir = Path(base)
    radar.watch_topics = list(watch)
    radar.profile_gaps = profile_gap_topics(base) if gaps is None else gaps
    return radar


def test_prompt_block_carries_gaps_only_when_there_are_some(base, tmp_path_factory):
    block = _radar(base).watch_block()
    assert "PROFILE GAPS (from the Profile review)" in block
    assert "- a/b testing - 9 of 15 pursued roles ask for it" in block
    empty = _radar(tmp_path_factory.mktemp("none")).watch_block()
    assert empty == ""
    watch_only = _radar(tmp_path_factory.mktemp("w"), watch=[{"topic": "ai evaluation", "note": ""}]).watch_block()
    assert "ACTIVE WATCH TOPICS" in watch_only and "PROFILE GAPS" not in watch_only


def test_picks_are_labelled_as_gaps_and_invented_gaps_are_not():
    arts = [{"url": "https://a.example/1"}]
    raw = [
        {"tier": "primary", "theme": "Experimentation", "watch_topic": "A/B Testing",
         "headline": "h1", "source_url": "https://a.example/1"},
        {"tier": "secondary", "theme": "Evaluation", "watch_topic": "ai evaluation",
         "headline": "h2", "source_url": "https://a.example/1"},
        {"tier": "supporting", "theme": "Other", "watch_topic": "quantum marketing",
         "headline": "h3", "source_url": "https://a.example/1"},
    ]
    picks = content_radar.ContentRadar._clean_picks(
        raw, arts, [{"topic": "ai evaluation"}], [{"topic": "a/b testing", "note": "x"}])
    assert [(p["watch_topic"], p["profile_gap"]) for p in picks] == [
        ("a/b testing", True), ("ai evaluation", False), ("", False)]


def test_radar_page_labels_gap_picks_and_lists_the_gaps():
    import build
    d = {"date": "2026-09-28", "window_days": 7, "profile_gaps": ["a/b testing", "team leadership"],
         "stats": {"articles_fetched": 1, "sources_with_entries": 1, "sources_configured": 1, "llm": ""},
         "sections": {"00_filter": {"text": "f", "title": "t"},
                      "01_pillar_picks": {"title": "t", "picks": [
                          {"tier": "primary", "theme": "Experimentation", "watch_topic": "a/b testing",
                           "profile_gap": True, "headline": "H", "why_it_matters": "w", "angle": "a",
                           "formats": ["LinkedIn post"], "source_name": "S", "source_url": "u",
                           "source_date": "2026-09-27"}], "note": ""},
                      "02_network_signal": {"summary": "", "rows": [], "title": "t"},
                      "03_repo_signal": {"summary": "", "repos": [], "title": "t"},
                      "04_queue": {"items": [], "title": "t"}}}
    html = build._env().get_template("radar.html.j2").render(d=d, picks=build._radar_picks(d))
    assert "profile gap: a/b testing" in html
    assert "Asked to cover your profile gaps: a/b testing, team leadership" in html
    md = content_radar.ContentRadar.render_markdown({**d, "stats": {**d["stats"], "picks": 1}})
    assert "**Profile gap:** a/b testing" in md


def test_picks_are_capped_at_the_target():
    arts = [{"url": f"u{i}"} for i in range(12)]
    raw = [{"tier": "supporting", "theme": f"Theme {i}", "source_url": f"u{i}"} for i in range(12)]
    picks = content_radar.ContentRadar._clean_picks(raw, arts)
    assert len(picks) == content_radar.TARGET_PICKS == 9


def test_radar_page_lists_every_candidate_title_source_and_link():
    import build
    d = {"date": "2026-09-28", "window_days": 14, "profile_gaps": [],
         "stats": {"articles_fetched": 2, "sources_with_entries": 1, "sources_configured": 1, "llm": ""},
         "sections": {"00_filter": {"text": "f", "title": "t"},
                      "01_pillar_picks": {"title": "t", "picks": [], "note": ""},
                      "02_network_signal": {"summary": "", "rows": [], "title": "t"},
                      "03_repo_signal": {"summary": "", "repos": [], "title": "t"},
                      "04_queue": {"items": [], "title": "t"}},
         "raw_articles": [
             {"source": "Example Feed", "category": "C", "title": "First Example Post",
              "url": "https://example.com/one", "published": "2026-09-27", "chars": 900},
             {"source": "Other Feed", "category": "C", "title": "Second Example Post",
              "url": "https://example.com/two", "published": "2026-09-26", "chars": 900}]}
    html = build._env().get_template("radar.html.j2").render(d=d, picks=build._radar_picks(d))
    assert "All 2 articles the picks were chosen from" in html
    for a in d["raw_articles"]:
        assert f'href="{a["url"]}"' in html and a["title"] in html and a["source"] in html
    md = content_radar.ContentRadar.render_markdown({**d, "stats": {**d["stats"], "picks": 0}})
    assert "- [First Example Post](https://example.com/one) - Example Feed" in md

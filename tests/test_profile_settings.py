"""Profile settings: agents/profile_settings.py and the dashboard's /api/settings.

The profile.md fixture is made up but carries the shapes a real one grows:
a wrapped hint, a parked keyword inside a comment in the MIDDLE of a list,
group labels between bullets, quoted keywords and a bullet that wraps.
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

import dashboard as srv  # noqa: E402
import profile_settings as ps  # noqa: E402
from job_intel import load_archetypes  # noqa: E402
from profile_keywords import load_keywords, load_watch_topics  # noqa: E402

PROFILE = """# Career Goals

<!-- ground truth -->

## Target Roles
*(In descending priority order)*

1. Head of Widgets
2. Director, Gadget Analytics

<!-- 3. Parked role -->

## Key Technical Areas
*(Core skills)*

Core:

- widget modelling
- gadget forecasting

Adjacent:

- sprocket design

## Domain Expertise

<!-- a few sentences -->

I build widget models for mid-size gadget makers.

## Target Keywords
*(Raise a role's score. Order matters for the
  board searches, so the strongest lead.)*

- widget science
- gadget analytics
<!-- parked:
- sprocket theory
-->
- forecasting

## Watch Topics
*(Emerging themes)*

- tiny robots: they are coming
- quiet gears

## Exclude Keywords
*(Title only)*

- "junior"
- intern

## Career Positioning

- Widget leader who ships

## Notes

- Clean rooms are a gap
  I am closing this year
"""


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "me").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "me/profile.md").write_text(PROFILE)
    return tmp_path


def _text(ws):
    return (ws / "me/profile.md").read_text()


def test_reads_every_section(ws):
    g = ps.read_goals(ws)
    assert g["target_roles"] == ["Head of Widgets", "Director, Gadget Analytics"]
    assert g["technical_areas"] == ["widget modelling", "gadget forecasting", "sprocket design"]
    assert g["target_keywords"] == ["widget science", "gadget analytics", "forecasting"]
    assert g["domain_expertise"] == "I build widget models for mid-size gadget makers."
    assert g["persona"] == ""
    assert "I am closing this year" in g["notes"]


def test_saving_unchanged_values_leaves_the_file_alone(ws):
    for key, value in ps.read_goals(ws).items():
        ps.set_goal(ws, key, value)
    assert _text(ws) == PROFILE
    assert not (ws / "me/profile.prev.md").exists()


def test_list_edit_touches_only_its_items(ws):
    before = load_keywords(ws)
    ps.set_goal(ws, "target_keywords", ["gadget analytics", "widget science", "forecasting", "gear ratios"])
    text = _text(ws)
    assert load_keywords(ws)["target"] == ["gadget analytics", "widget science", "forecasting", "gear ratios"]
    assert load_keywords(ws)["exclude"] == before["exclude"]
    assert "- sprocket theory\n-->" in text                 # the parked keyword survives
    assert "Order matters for the\n  board searches" in text  # so does the wrapped hint
    assert text.index("<!-- parked:") < text.index("- forecasting")  # and stays in place
    assert [t["topic"] for t in load_watch_topics(ws)] == ["tiny robots", "quiet gears"]
    assert (ws / "me/profile.prev.md").read_text() == PROFILE


def test_group_labels_stay_put_when_a_list_shrinks(ws):
    ps.set_goal(ws, "technical_areas", ["widget modelling"])
    text = _text(ws)
    assert "Core:" in text and "Adjacent:" in text
    assert ps.read_goals(ws)["technical_areas"] == ["widget modelling"]


def test_target_roles_are_renumbered_and_feed_role_cat(ws):
    ps.set_goal(ws, "target_roles", ["Director, Gadget Analytics", "Head of Widgets", "VP Sprockets"])
    assert "1. Director, Gadget Analytics\n2. Head of Widgets\n3. VP Sprockets" in _text(ws)
    assert [a["label"] for a in load_archetypes(ws)] == [
        "01 Director, Gadget Analytics", "02 Head of Widgets", "03 VP Sprockets"]
    assert "<!-- 3. Parked role -->" in _text(ws)


def test_keywords_are_stored_bare_and_deduplicated(ws):
    ps.set_goal(ws, "exclude_keywords", ['"junior"', "intern", "Intern", "- senior intern", ""])
    assert ps.read_goals(ws)["exclude_keywords"] == ["junior", "intern", "senior intern"]


def test_prose_keeps_its_preamble_and_a_missing_section_is_added(ws):
    ps.set_goal(ws, "domain_expertise", "Two lines.\nOf prose.")
    assert "<!-- a few sentences -->\n\nTwo lines.\nOf prose." in _text(ws)
    ps.set_goal(ws, "persona", "Plain spoken, evidence first.")
    assert _text(ws).rstrip().endswith("## Persona\n\nPlain spoken, evidence first.")
    assert ps.read_goals(ws)["persona"] == "Plain spoken, evidence first."
    assert ps.read_goals(ws)["notes"].startswith("- Clean rooms")


def test_starts_from_the_template_when_profile_md_is_missing(tmp_path):
    ps.set_goal(tmp_path, "target_roles", ["Head of Widgets"])
    text = (tmp_path / "me/profile.md").read_text()
    assert "## Watch Topics" in text and "1. Head of Widgets" in text


@pytest.mark.parametrize("key,value", [
    ("notes", "## Sneaky new section"),
    ("notes", "text <!-- open comment"),
    ("target_keywords", ["fine", "<!-- nope"]),
    ("target_keywords", "not a list"),
    ("target_roles", ["x" * 500]),
    ("target_roles", [f"role {i}" for i in range(50)]),
    ("no_such_section", []),
])
def test_bad_goal_values_are_refused(ws, key, value):
    with pytest.raises(ValueError):
        ps.set_goal(ws, key, value)
    assert _text(ws) == PROFILE


def test_accounts_write_the_workspace_env_and_never_the_password(ws):
    (ws / ".env").write_text("BLUESKY_APP_PASSWORD=made-up-app-pass\n")
    assert ps.set_account(ws, "github_username", "@octo-widget") == ("", "octo-widget")
    ps.set_account(ws, "bluesky_handle", "Widget.bsky.social")
    acc = ps.read_accounts(ws)
    assert acc == {"github_username": "octo-widget", "bluesky_handle": "widget.bsky.social",
                   "bluesky_app_password_set": True}
    assert "made-up-app-pass" not in json.dumps(acc)
    assert "BLUESKY_APP_PASSWORD=made-up-app-pass" in (ws / ".env").read_text()
    for key, value in [("github_username", "bad name"), ("github_username", "-lead"),
                       ("bluesky_handle", "nodots"), ("bluesky_app_password", "x")]:
        with pytest.raises(ValueError):
            ps.set_account(ws, key, value)


def test_review_writes_the_workspace_copy_only(ws):
    tracked = (REPO / "config/profile.yaml").read_text()
    ps.set_review(ws, "pursued_min_fit", "65")
    ps.set_review(ws, "sites", [{"url": "https://widgets.example/", "max_pages": "2"}, {"url": ""}])
    assert (REPO / "config/profile.yaml").read_text() == tracked
    r = ps.read_review(ws)
    assert r["pursued_min_fit"] == 65
    assert r["sites"] == [{"url": "https://widgets.example/", "max_pages": 2}]
    assert r["reevaluate_days"] == 7


def test_review_keeps_keys_it_does_not_know(ws):
    (ws / "config/profile.yaml").write_text("reevaluate_days: 3\nfuture_knob: blue\n")
    ps.set_review(ws, "linkedin_stale_days", 45)
    import yaml
    raw = yaml.safe_load((ws / "config/profile.yaml").read_text())
    assert raw["future_knob"] == "blue" and raw["reevaluate_days"] == 3 and raw["linkedin_stale_days"] == 45


@pytest.mark.parametrize("key,value", [
    ("pursued_min_fit", 101), ("reevaluate_days", 0), ("reevaluate_days", "soon"),
    ("sites", [{"url": "ftp://widgets.example"}]), ("sites", [{"url": "https://a.example", "max_pages": 999}]),
    ("sites", "https://a.example"), ("something_else", 1),
])
def test_bad_review_values_are_refused(ws, key, value):
    with pytest.raises(ValueError):
        ps.set_review(ws, key, value)
    assert not (ws / "config/profile.yaml").exists()


# ---------------------------------------------------------------- the API

@pytest.fixture
def api(ws):
    for sub in ("artifacts/html", "artifacts/jobs", "data"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    httpd = srv.serve(port=8812, base=ws, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield "http://127.0.0.1:8812", httpd
    httpd.shutdown()
    httpd.server_close()


def test_api_reads_and_writes_settings(api, ws):
    url, httpd = api
    got = requests.get(f"{url}/api/settings", timeout=5).json()
    assert got["goals"]["target_roles"] == ["Head of Widgets", "Director, Gadget Analytics"]

    r = requests.post(f"{url}/api/settings", json={"group": "goals", "field": "target_keywords",
                                                   "value": ['"widget science"', "gear ratios"]}, timeout=5)
    assert r.status_code == 200 and r.json()["changed"] and r.json()["value"] == ["widget science", "gear ratios"]
    changes = requests.get(f"{url}/api/changes", timeout=5).json()["changes"]
    assert any(c["field"] == "settings:goals.target_keywords" for c in changes)

    again = requests.post(f"{url}/api/settings", json={"group": "goals", "field": "target_keywords",
                                                       "value": ["widget science", "gear ratios"]}, timeout=5)
    assert again.json()["changed"] is False


def test_api_refreshes_role_cat_after_a_target_roles_edit(api):
    url, httpd = api
    requests.post(f"{url}/api/settings", json={"group": "goals", "field": "target_roles",
                                               "value": ["VP Sprockets"]}, timeout=5)
    assert httpd.mc_editable["role_cat"] == ["", "01 VP Sprockets"]
    assert requests.get(f"{url}/api/health", timeout=5).json()["editable"]["role_cat"] == ["", "01 VP Sprockets"]


@pytest.mark.parametrize("payload", [
    {"group": "accounts", "field": "bluesky_app_password", "value": "x"},
    {"group": "review", "field": "pursued_min_fit", "value": 500},
    {"group": "nope", "field": "x", "value": 1},
    {"group": "goals", "field": "notes", "value": 12},
])
def test_api_refuses_bad_settings(api, payload):
    url, _ = api
    r = requests.post(f"{url}/api/settings", json=payload, timeout=5)
    assert r.status_code == 400 and r.json()["error"]


def test_settings_page_is_served_live(api):
    url, _ = api
    page = requests.get(f"{url}/settings.html", timeout=5)
    assert page.status_code == 200
    assert 'id="settings-data"' in page.text and "Head of Widgets" in page.text
    assert 'href="settings.html"' in page.text

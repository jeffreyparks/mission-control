"""Site collector: sections, stripping, robots.txt, sitemap and size caps, and the
site's place in the Profile stage. No network - every HTTP call is faked."""
import sys
from pathlib import Path

import pytest
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO))

import profile_snapshot  # noqa: E402
import site_scanner  # noqa: E402
from site_scanner import SiteScanner, normalise_url, parse_page  # noqa: E402

ROOT = "https://me.example/"

PAGE = """<!doctype html><html><head>
<title>Jo Doe | Measurement</title>
<meta name="description" content="Causal inference and experiment design.">
<meta property="og:title" content="Jo Doe">
<style>.x{color:red}</style><script>var tracking = 1;</script>
</head><body>
<header class="masthead"><h1>Jo Doe</h1><p>I make marketing measurement causal.</p>
  <div><span>10+ yrs</span><span>$500M+ media</span></div></header>
<nav><a href="/">Home</a><a href="#about">About</a></nav>
<section id="about"><h2>About</h2><p>I lead measurement teams.</p>
  <p>See <a href="https://github.com/jodoe">my GitHub</a>.</p></section>
<section id="work"><h2>Selected Work</h2><ul><li>mmm-adstock: adstock curves</li></ul></section>
<footer><a href="https://linkedin.com/in/jodoe">LinkedIn</a> &copy; 2026</footer>
<!-- a comment that should never show up -->
</body></html>"""


class FakeResponse:
    def __init__(self, text="", status=200, content_type="text/html; charset=utf-8"):
        self.text, self.status_code = text, status
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **_):
        self.calls.append(url)
        answer = self.routes.get(url)
        if answer is None:
            return FakeResponse(status=404)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _scanner(routes, **kw):
    return SiteScanner(ROOT, ".", session=FakeSession(routes), **kw)


# ---------- parsing ----------

def test_page_is_split_into_sections_with_chrome_stripped():
    page = parse_page(PAGE, ROOT)
    assert page["identity"]["title"] == "Jo Doe | Measurement"
    assert page["identity"]["description"] == "Causal inference and experiment design."
    assert page["identity"]["h1"] == "Jo Doe"
    assert page["identity"]["hero"] == "I make marketing measurement causal.\n\n10+ yrs $500M+ media"

    by_title = {s["title"]: s["text"] for s in page["sections"]}
    assert list(by_title) == ["About", "Selected Work"]
    assert by_title["About"] == "I lead measurement teams.\n\nSee my GitHub."
    all_text = " ".join(by_title.values())
    for gone in ("tracking", "color:red", "Home", "LinkedIn", "comment", "Jo Doe"):
        assert gone not in all_text

    # links are read before the footer is stripped
    assert page["links"] == ["https://github.com/jodoe", "https://linkedin.com/in/jodoe"]


def test_text_before_the_first_h2_is_an_intro_section():
    page = parse_page("<html><body><p>Hello there.</p><h2>More</h2><p>Detail</p></body></html>", ROOT)
    assert [(s["slug"], s["title"]) for s in page["sections"]] == [("intro", ""), ("more", "More")]


def test_repeated_headings_get_distinct_ids():
    page = parse_page("<body><h2>Notes</h2><p>a</p><h2>Notes</h2><p>b</p></body>", ROOT)
    assert [s["slug"] for s in page["sections"]] == ["notes", "notes-2"]


def test_normalise_url():
    assert normalise_url("https://me.example") == "https://me.example/"
    assert normalise_url("https://me.example/work#top") == "https://me.example/work"


# ---------- collect ----------

def test_snapshot_of_a_one_pager():
    session = FakeSession({ROOT: FakeResponse(PAGE)})
    snap = SiteScanner(ROOT, ".", session=session).collect()

    assert snap["source_key"] == "site:https://me.example/"
    assert [i["kind"] for i in snap["items"]] == ["page", "section", "section"]
    assert snap["items"][1]["id"] == "https://me.example/#about"
    assert snap["stats"]["pages_fetched"] == 1 and snap["stats"]["truncated"] is False
    assert not any("sitemap" in c for c in session.calls)  # max_pages 0: never asked


def test_same_page_gives_the_same_hash():
    first = _scanner({ROOT: FakeResponse(PAGE)}).collect()
    second = _scanner({ROOT: FakeResponse(PAGE)}).collect()
    assert first["hash"] == second["hash"]
    edited = _scanner({ROOT: FakeResponse(PAGE.replace("teams", "people"))}).collect()
    assert edited["hash"] != first["hash"]


def test_robots_disallow_skips_the_site():
    snap = _scanner({
        ROOT + "robots.txt": FakeResponse("User-agent: *\nDisallow: /\n", content_type="text/plain"),
        ROOT: FakeResponse(PAGE),
    }).collect()
    assert snap is None


def test_robots_wrapped_in_html_is_still_read():
    wrapped = ("<!DOCTYPE html><html><head><title>Me</title></head><body>\n"
               "User-agent: *\nDisallow: /\n</body></html>")
    assert _scanner({ROOT + "robots.txt": FakeResponse(wrapped), ROOT: FakeResponse(PAGE)}).collect() is None

    allowing = wrapped.replace("Disallow: /", "Allow: /")
    snap = _scanner({ROOT + "robots.txt": FakeResponse(allowing), ROOT: FakeResponse(PAGE)}).collect()
    assert snap is not None


def test_sitemap_pages_are_capped_and_kept_on_host():
    sitemap = ("<urlset><url><loc>https://me.example/</loc></url>"
               "<url><loc>https://me.example/a</loc></url>"
               "<url><loc>https://elsewhere.example/x</loc></url>"
               "<url><loc>https://me.example/b</loc></url>"
               "<url><loc>https://me.example/c</loc></url></urlset>")
    other = "<html><head><title>Other</title></head><body><h2>Post</h2><p>words</p></body></html>"
    routes = {ROOT + "sitemap.xml": FakeResponse(sitemap, content_type="application/xml"),
              ROOT: FakeResponse(PAGE),
              "https://me.example/a": FakeResponse(other),
              "https://me.example/b": FakeResponse(other)}
    session = FakeSession(routes)
    snap = SiteScanner(ROOT, ".", max_pages=2, session=session).collect()

    pages = [i["id"] for i in snap["items"] if i["kind"] == "page"]
    assert pages == [ROOT, "https://me.example/a", "https://me.example/b"]
    assert snap["identity"]["title"] == "Jo Doe | Measurement"  # identity comes from the root
    assert "https://me.example/c" not in session.calls


def test_text_cap_truncates_and_says_so(monkeypatch):
    monkeypatch.setattr(site_scanner, "MAX_TEXT_CHARS", 30)
    snap = _scanner({ROOT: FakeResponse(PAGE)}).collect()
    kept = sum(len(i["text"]) for i in snap["items"])
    assert kept == 30 and snap["stats"]["truncated"] is True


def test_unreachable_site_raises():
    with pytest.raises(requests.ConnectionError):
        _scanner({ROOT: requests.ConnectionError("no route")}).collect()


# ---------- config and the stage ----------

def _workspace(tmp_path, config):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/profile.yaml").write_text(config)
    (tmp_path / "me/linkedin").mkdir(parents=True)
    return tmp_path


def test_config_reads_the_workspace_copy_and_fills_defaults(tmp_path):
    base = _workspace(tmp_path, "sites:\n  - url: https://me.example\n  - https://two.example/\n"
                                "linkedin_stale_days: 14\n")
    config = profile_snapshot.load_config(base)
    assert config["sites"] == [{"url": "https://me.example", "max_pages": 0},
                               {"url": "https://two.example/", "max_pages": 0}]
    assert config["linkedin_stale_days"] == 14
    assert config["pursued_window_days"] == 60


def test_config_falls_back_to_the_repo_default(tmp_path):
    config = profile_snapshot.load_config(tmp_path)
    assert config["sites"] == [] and config["linkedin_stale_days"] == 30


def test_stage_runs_sites_and_survives_an_unreachable_one(tmp_path, monkeypatch):
    base = _workspace(tmp_path, "sites:\n  - url: https://me.example/\n  - url: https://down.example/\n")
    routes = {ROOT: FakeResponse(PAGE),
              "https://down.example/": requests.ConnectionError("no route")}
    monkeypatch.setattr(site_scanner.requests, "Session", lambda: FakeSession(routes))

    found, skipped = profile_snapshot.collectors(base, env={})
    assert [label for label, _ in found] == ["linkedin", "site https://me.example/",
                                            "site https://down.example/"]

    lines = []
    result = profile_snapshot.run_profile_stage(base, log=lines.append, env={})
    assert result["changed"] == ["site:https://me.example/"]
    assert result["failed"] == ["site https://down.example/"]
    assert "linkedin" in result["skipped"]  # no export in this workspace

    again = profile_snapshot.run_profile_stage(base, log=lines.append, env={})
    assert again["unchanged"] == ["site:https://me.example/"]

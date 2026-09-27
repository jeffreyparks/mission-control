"""Profile snapshots: collectors, the fingerprint, change-only storage, and the
home page's LinkedIn coverage number. No network - every HTTP call is faked."""
import csv
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import profile_snapshot  # noqa: E402
from bluesky_scanner import BlueSkyScanner  # noqa: E402
from github_scanner import GitHubScanner  # noqa: E402
from history import History  # noqa: E402
from linkedin_scanner import LinkedInScanner  # noqa: E402
from profile_snapshot import fingerprint, make_snapshot  # noqa: E402
from store import Store  # noqa: E402

import requests  # noqa: E402


# ---------- fakes ----------

class FakeResponse:
    def __init__(self, payload=None, status=200, text=None):
        self.payload, self.status_code = payload, status
        self.text = text if text is not None else ""

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class FakeSession:
    """Answers GET/POST by the first route whose key is a substring of the URL."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _answer(self, url):
        self.calls.append(url)
        for key, answer in self.routes.items():
            if key in url:
                return answer() if callable(answer) else answer
        return FakeResponse(status=404)

    def get(self, url, **_):
        return self._answer(url)

    def post(self, url, **_):
        return self._answer(url)


@pytest.fixture
def base(tmp_path):
    (tmp_path / "me/linkedin").mkdir(parents=True)
    (tmp_path / "me/profile.md").write_text(
        "# Profile\n\n## Target Keywords\n- experiment design\n- causal inference\n"
        "- marketing science\n- python\n")
    return tmp_path


# ---------- fingerprint ----------

def _snap(**overrides):
    items = [{"kind": "post", "id": "p1", "date": "2026-09-01", "text": "On causal inference",
              "signals": {"likes": 3}}]
    snap = make_snapshot("bluesky", "bluesky:me", {"bio": "Measurement lead"}, items,
                         {"followers": 10})
    snap.update(overrides)
    return snap


def test_counts_and_capture_time_do_not_change_the_fingerprint():
    a = _snap()
    b = _snap()
    b["items"][0]["signals"]["likes"] = 999
    b["stats"]["followers"] = 5000
    b["captured_at"] = "2030-01-01T00:00:00"
    assert fingerprint(a) == fingerprint(b)


def test_words_change_the_fingerprint():
    a = _snap()
    b = _snap()
    b["identity"]["bio"] = "Measurement lead, marketing science"
    c = _snap()
    c["items"][0]["text"] = "On experiment design"
    assert len({fingerprint(a), fingerprint(b), fingerprint(c)}) == 3


def test_empty_identity_fields_are_dropped():
    snap = make_snapshot("github", "github:x", {"bio": "", "name": None, "blog": "x.dev"}, [])
    assert snap["identity"] == {"blog": "x.dev"}


# ---------- storage ----------

def test_store_keeps_a_snapshot_only_when_it_changed(base):
    store = Store(base)
    changed, first_id = store.save_snapshot_if_changed(_snap())

    noisy = _snap()
    noisy["items"][0]["signals"]["likes"] = 50
    noisy["hash"] = fingerprint(noisy)
    again, same_id = store.save_snapshot_if_changed(noisy)

    edited = _snap()
    edited["identity"]["bio"] = "New bio"
    edited["hash"] = fingerprint(edited)
    third, new_id = store.save_snapshot_if_changed(edited)

    assert (changed, again, third) == (True, False, True)
    assert same_id == first_id and new_id != first_id
    assert store.latest_snapshot("bluesky:me")["identity"]["bio"] == "New bio"
    assert store.get_meta("profile_checked:bluesky:me") == edited["captured_at"]
    assert [s["source_key"] for s in store.latest_snapshots()] == ["bluesky:me"]


# ---------- GitHub ----------

def _github_routes(pinned=None):
    repos = [
        {"name": "measurement-kit", "full_name": "me/measurement-kit", "fork": False,
         "description": "Experiment design helpers", "topics": ["causal-inference"],
         "language": "Python", "created_at": "2025-01-02T00:00:00Z",
         "pushed_at": "2026-09-20T00:00:00Z", "stargazers_count": 4, "forks_count": 1,
         "html_url": "https://github.com/me/measurement-kit"},
        {"name": "me", "full_name": "me/me", "fork": False, "description": "",
         "created_at": "2024-01-01T00:00:00Z", "pushed_at": "2026-01-01T00:00:00Z"},
        {"name": "someone-elses", "full_name": "me/someone-elses", "fork": True,
         "created_at": "2024-01-01T00:00:00Z", "pushed_at": "2026-09-25T00:00:00Z"},
    ]
    routes = {
        "users/me/repos": FakeResponse(repos),
        "users/me": FakeResponse({"name": "Me", "bio": "Measurement lead", "followers": 7,
                                  "public_repos": 3}),
        "repos/me/me/readme": FakeResponse(text="Hi, I build measurement systems."),
        "repos/me/measurement-kit/readme": FakeResponse(text="# measurement-kit\nA/B tests."),
    }
    if pinned is not None:
        routes["graphql"] = FakeResponse({"data": {"user": {"pinnedItems": {
            "nodes": [{"nameWithOwner": n} for n in pinned]}}}})
    return routes


def test_github_snapshot_shape_without_token(base):
    session = FakeSession(_github_routes())
    snap = GitHubScanner("me", base, token="", session=session).collect()

    assert snap["source_key"] == "github:me"
    assert snap["identity"]["readme"].startswith("Hi, I build")
    assert [i["id"] for i in snap["items"]] == ["me/measurement-kit"]  # no fork, no profile repo
    repo = snap["items"][0]
    assert repo["meta"]["featured"] and not repo["meta"]["pinned"]
    assert "A/B tests." in repo["text"]
    assert repo["signals"]["stars"] == 4
    assert snap["stats"]["forks_skipped"] == 1
    assert snap["stats"]["pinned_source"] == "recent pushes"
    assert not any("graphql" in c for c in session.calls)


def test_github_uses_pinned_repos_with_a_token(base):
    session = FakeSession(_github_routes(pinned=["me/measurement-kit"]))
    snap = GitHubScanner("me", base, token="t0ken", session=session).collect()
    assert snap["items"][0]["meta"]["pinned"] is True
    assert snap["stats"]["pinned_source"] == "pinned"


def test_github_star_changes_do_not_change_the_hash(base):
    first = GitHubScanner("me", base, token="", session=FakeSession(_github_routes())).collect()
    routes = _github_routes()
    routes["users/me/repos"].payload[0]["stargazers_count"] = 400
    routes["users/me/repos"].payload[0]["pushed_at"] = "2026-09-26T00:00:00Z"
    second = GitHubScanner("me", base, token="", session=FakeSession(routes)).collect()
    assert first["hash"] == second["hash"]


# ---------- BlueSky ----------

FEED = {"feed": [
    {"post": {"uri": "at://did:plc:me/app.bsky.feed.post/aaa", "author": {"handle": "me.bsky.social"},
              "record": {"text": "Notes on experiment design", "createdAt": "2026-09-20T10:00:00Z"},
              "likeCount": 5}},
    {"post": {"uri": "at://did:plc:me/app.bsky.feed.post/bbb", "author": {"handle": "me.bsky.social"},
              "record": {"text": "agreed", "createdAt": "2026-09-21T10:00:00Z",
                         "reply": {"parent": {}}}}},
    {"post": {"uri": "at://did:plc:x/app.bsky.feed.post/ccc", "author": {"handle": "x.bsky.social"},
              "record": {"text": "Someone else's take", "createdAt": "2026-09-10T10:00:00Z"}},
     "reason": {"$type": "app.bsky.feed.defs#reasonRepost", "indexedAt": "2026-09-22T09:00:00Z"}},
]}
PROFILE = {"displayName": "Me", "description": "Measurement lead", "followersCount": 12,
           "postsCount": 40}


def test_bluesky_reads_the_public_api_and_weights_reposts(base):
    session = FakeSession({"getProfile": FakeResponse(PROFILE), "getAuthorFeed": FakeResponse(FEED)})
    snap = BlueSkyScanner("@me.bsky.social", base, env={}, session=session).collect()

    assert all(c.startswith("https://public.api.bsky.app/xrpc/") for c in session.calls)
    assert snap["source_key"] == "bluesky:me.bsky.social"
    assert [i["kind"] for i in snap["items"]] == ["post", "reply", "repost"]
    assert [i["weight"] for i in snap["items"]] == [1.0, 1.0, 0.5]
    repost = snap["items"][2]
    assert repost["date"] == "2026-09-22" and repost["meta"]["author"] == "x.bsky.social"
    assert snap["items"][0]["url"] == "https://bsky.app/profile/me.bsky.social/post/aaa"
    assert snap["stats"]["last_activity"] == "2026-09-21"  # reposts are not your activity
    assert snap["stats"]["read_via"] == "public"


def test_bluesky_falls_back_to_the_app_password(base):
    public_down = FakeResponse(status=502)
    session = FakeSession({
        "public.api.bsky.app": public_down,
        "createSession": FakeResponse({"accessJwt": "jwt"}),
        "pds.example/xrpc/app.bsky.actor.getProfile": FakeResponse(PROFILE),
        "pds.example/xrpc/app.bsky.feed.getAuthorFeed": FakeResponse(FEED),
    })
    env = {"BLUESKY_APP_PASSWORD": "pw", "BLUESKY_PDS_URL": "https://pds.example"}
    snap = BlueSkyScanner("me.bsky.social", base, env=env, session=session).collect()
    assert snap["stats"]["read_via"] == "app password"
    assert len(snap["items"]) == 3


def test_bluesky_without_a_password_surfaces_the_public_failure(base):
    session = FakeSession({"public.api.bsky.app": FakeResponse(status=502)})
    with pytest.raises(requests.HTTPError):
        BlueSkyScanner("me.bsky.social", base, env={}, session=session).collect()


# ---------- LinkedIn ----------

def _csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def _write_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Profile.csv", _csv([{
            "First Name": "Jo", "Last Name": "Doe", "Headline": "Head of Marketing Science",
            "Summary": "I run experiment design programs.", "Industry": "Software",
            "Geo Location": "Boston"}]))
        archive.writestr("Positions.csv", _csv([{
            "Title": "Director", "Company Name": "Tata", "Description": "Built the causal team",
            "Started On": "Mar 2021", "Finished On": ""}]))
        archive.writestr("Skills.csv", _csv([{"Name": "Python"}]))
        archive.writestr("Posts.csv", _csv([{
            "Date": "2026-09-01 10:00:00", "ShareLink": "https://lnkd.in/p1",
            "ShareCommentary": "Why marketing science needs holdouts", "SharedUrl": ""}]))


def test_linkedin_zip_snapshot(base):
    _write_zip(base / "me/linkedin/linkedin-export.zip")
    snap = LinkedInScanner(base).collect()

    assert snap["source_key"] == "linkedin:export"
    assert snap["identity"]["headline"] == "Head of Marketing Science"
    assert snap["identity"]["about"].startswith("I run")
    kinds = sorted(i["kind"] for i in snap["items"])
    assert kinds == ["position", "post", "skill"]
    position = next(i for i in snap["items"] if i["kind"] == "position")
    assert position["title"] == "Director at Tata" and position["date"] == "2021-03-01"
    assert snap["stats"]["stale"] is False and snap["stats"]["export_file"] == "linkedin-export.zip"


def test_linkedin_stale_export_is_flagged(base):
    path = base / "me/linkedin/linkedin-export.zip"
    _write_zip(path)
    old = time.time() - 45 * 86400
    os.utime(path, (old, old))
    snap = LinkedInScanner(base).collect()
    assert snap["stats"]["stale"] is True and snap["stats"]["age_days"] >= 45


def test_linkedin_pdf_snapshot(base, monkeypatch):
    pdf_text = "Jo Doe\nHead of Marketing Science\nTop Skills\nPython\nCausal Inference\nExperience\nDirector"

    class Page:
        def extract_text(self):
            return pdf_text

    class Reader:
        def __init__(self, _path):
            self.pages = [Page()]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    (base / "me/linkedin/profile.pdf").write_bytes(b"%PDF-1.4 stub")

    snap = LinkedInScanner(base).collect()
    assert snap["identity"] == {"name": "Jo Doe", "headline": "Head of Marketing Science"}
    assert [i["title"] for i in snap["items"] if i["kind"] == "skill"] == ["Python", "Causal Inference"]
    assert snap["stats"]["export_file"] == "profile.pdf"


def test_linkedin_newer_export_wins(base, monkeypatch):
    zip_path = base / "me/linkedin/linkedin-export.zip"
    _write_zip(zip_path)
    pdf_path = base / "me/linkedin/profile.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 stub")
    old = time.time() - 10 * 86400
    os.utime(pdf_path, (old, old))
    assert LinkedInScanner(base).collect()["stats"]["export_file"] == "linkedin-export.zip"


def test_linkedin_without_export_is_skipped(base):
    assert LinkedInScanner(base).collect() is None


# ---------- the stage ----------

def test_stage_stores_changes_and_isolates_failures(base, monkeypatch):
    _write_zip(base / "me/linkedin/linkedin-export.zip")

    def broken():
        raise RuntimeError("github is down")

    def fake_collectors(base_dir, env=None):
        from linkedin_scanner import LinkedInScanner as L
        return [("github", broken), ("linkedin", L(base_dir).collect)], ["bluesky (no BLUESKY_HANDLE)"]

    monkeypatch.setattr(profile_snapshot, "collectors", fake_collectors)
    lines = []
    first = profile_snapshot.run_profile_stage(base, log=lines.append)
    second = profile_snapshot.run_profile_stage(base, log=lines.append)

    assert first["changed"] == ["linkedin:export"] and first["failed"] == ["github"]
    assert second["changed"] == [] and second["unchanged"] == ["linkedin:export"]
    assert "bluesky (no BLUESKY_HANDLE)" in first["skipped"]
    assert profile_snapshot.summary(second) == "profiles: 0 changed, 1 unchanged, failed: github"
    assert not (base / "artifacts/profiles").exists() or not list((base / "artifacts/profiles").iterdir())


def test_collectors_follow_the_workspace_identity(base):
    found, skipped = profile_snapshot.collectors(base, env={"GITHUB_USERNAME": "me"})
    assert [label for label, _ in found] == ["github", "linkedin"]
    assert skipped == ["bluesky (no BLUESKY_HANDLE)"]


# ---------- home page coverage ----------

def test_history_reads_linkedin_coverage_from_the_snapshot(base):
    _write_zip(base / "me/linkedin/linkedin-export.zip")
    Store(base).save_snapshot_if_changed(LinkedInScanner(base).collect())

    history = History(base)
    assert history.snapshot_linkedin_capture(date="2026-09-27") == 1
    row = history.conn.execute(
        "SELECT coverage_pct FROM profile_snapshot WHERE date='2026-09-27'").fetchone()
    # experiment design (about), causal (position text: 'causal team' - not 'causal inference'),
    # marketing science (headline), python (skill). The post's words do not count.
    assert row["coverage_pct"] == 75.0
    history.close()


def test_history_without_a_linkedin_snapshot_records_nothing(base):
    history = History(base)
    assert history.snapshot_linkedin_capture() == 0
    history.close()

"""
GitHub collector: what a visitor to your GitHub profile sees, as one snapshot.

Reads the public REST API. A GITHUB_TOKEN (optional, in the repo .env) raises the
rate limit and unlocks pinned repos, which need the GraphQL API. Without one the
"featured" repos fall back to the six most recently pushed.

Captured:
  identity  name, bio, company, blog, location, and the profile README
            (the <user>/<user> repo) - the first thing on the profile page
  items     one per non-fork public repo: description, topics, language, and
            the README for the featured six
  stats     followers, public repo count, last push - left out of the hash
"""
import os
from datetime import datetime
from pathlib import Path

import requests

from profile_snapshot import make_snapshot

API = "https://api.github.com"
FEATURED = 6
README_CHARS = 4000
TIMEOUT = 20


class GitHubScanner:
    def __init__(self, username, base_dir, token=None, session=None):
        self.username = username
        self.base_dir = Path(base_dir)
        self.token = (token if token is not None else os.environ.get("GITHUB_TOKEN", "")).strip()
        self.http = session or requests.Session()

    def _headers(self, accept="application/vnd.github+json"):
        headers = {"Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, endpoint):
        response = self.http.get(f"{API}/{endpoint}", headers=self._headers(), timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()

    def _readme(self, full_name):
        """A repo's README as raw text, or "" when it has none."""
        response = self.http.get(f"{API}/repos/{full_name}/readme",
                                 headers=self._headers("application/vnd.github.raw"),
                                 timeout=TIMEOUT)
        if response.status_code == 404:
            return ""
        response.raise_for_status()
        return response.text[:README_CHARS]

    def _pinned(self):
        """Names of pinned repos (owner/name). Needs a token; [] without one."""
        if not self.token:
            return []
        query = ("query($login:String!){user(login:$login){pinnedItems(first:6,types:REPOSITORY)"
                 "{nodes{... on Repository{nameWithOwner}}}}}")
        response = self.http.post(f"{API}/graphql", headers=self._headers(), timeout=TIMEOUT,
                                  json={"query": query, "variables": {"login": self.username}})
        response.raise_for_status()
        user = (response.json().get("data") or {}).get("user") or {}
        return [n["nameWithOwner"] for n in (user.get("pinnedItems") or {}).get("nodes", []) if n]

    def collect(self):
        profile = self._get(f"users/{self.username}")
        repos = self._get(f"users/{self.username}/repos?sort=pushed&per_page=100")
        own = [r for r in repos if not r.get("fork")]

        pinned = self._pinned()
        by_push = sorted(own, key=lambda r: r.get("pushed_at") or "", reverse=True)
        featured = pinned or [r["full_name"] for r in by_push[:FEATURED]]

        identity = {
            "name": profile.get("name"),
            "bio": profile.get("bio"),
            "company": profile.get("company"),
            "blog": profile.get("blog"),
            "location": profile.get("location"),
            "readme": self._readme(f"{self.username}/{self.username}"),
        }

        items = []
        for repo in own:
            if repo["name"].lower() == self.username.lower():
                continue  # the profile README repo is identity, not a project
            full = repo["full_name"]
            is_featured = full in featured
            text = repo.get("description") or ""
            if is_featured:
                readme = self._readme(full)
                text = f"{text}\n\n{readme}".strip() if readme else text
            items.append({
                "kind": "repo",
                "id": full,
                "date": (repo.get("created_at") or "")[:10] or None,
                "title": repo["name"],
                "text": text,
                "url": repo.get("html_url"),
                "meta": {
                    "language": repo.get("language"),
                    "topics": sorted(repo.get("topics") or []),
                    "featured": is_featured,
                    "pinned": full in pinned,
                },
                "signals": {
                    "stars": repo.get("stargazers_count", 0),
                    "forks": repo.get("forks_count", 0),
                    "pushed_at": repo.get("pushed_at"),
                },
            })
        items.sort(key=lambda i: (not i["meta"]["featured"], i["id"]))

        pushes = [r.get("pushed_at") for r in own if r.get("pushed_at")]
        last_push = max(pushes) if pushes else None
        stats = {
            "followers": profile.get("followers", 0),
            "public_repos": profile.get("public_repos", 0),
            "forks_skipped": len(repos) - len(own),
            "last_activity": last_push[:10] if last_push else None,
            "days_since_activity": _days_since(last_push),
            "pinned_source": "pinned" if pinned else "recent pushes",
        }
        return make_snapshot("github", f"github:{self.username}", identity, items, stats)


def _days_since(stamp):
    if not stamp:
        return None
    try:
        then = datetime.strptime(stamp[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return (datetime.now() - then).days


if __name__ == "__main__":
    import json
    import sys
    snap = GitHubScanner(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GITHUB_USERNAME", ""),
                         Path(__file__).resolve().parent.parent).collect()
    print(json.dumps({k: v for k, v in snap.items() if k != "items"}, indent=2))
    print(f"{len(snap['items'])} repos")

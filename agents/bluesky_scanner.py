"""
BlueSky collector: your public BlueSky voice, as one snapshot.

Reads the public AppView (public.api.bsky.app), which needs no login: your bio
and your last 100 feed entries. The AppView indexes every PDS in the network,
so a custom-PDS handle reads the same way. If the public read fails and an app
password is configured (BLUESKY_APP_PASSWORD, and BLUESKY_PDS_URL for a custom
PDS), the collector logs in and reads through your PDS instead.

Each feed entry is one item:
  post     your own post                                  weight 1.0
  reply    your reply to someone                          weight 1.0
  repost   someone else's post you reposted               weight 0.5

Reposts count toward what your profile says, at half weight: they are your
choice to amplify, but not your words. Like, repost and reply counts go in
`signals`, outside the fingerprint.
"""
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from profile_snapshot import make_snapshot

PUBLIC_API = "https://public.api.bsky.app/xrpc"
DEFAULT_PDS = "https://bsky.social"
FEED_LIMIT = 100
REPOST_WEIGHT = 0.5
TIMEOUT = 20


class BlueSkyScanner:
    def __init__(self, handle, base_dir, env=None, session=None):
        self.handle = handle.lstrip("@")
        self.base_dir = Path(base_dir)
        self.env = os.environ if env is None else env
        self.http = session or requests.Session()
        self.api_base = PUBLIC_API
        self.token = None

    # ---------- transport ----------

    def _get(self, endpoint, params):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = self.http.get(f"{self.api_base}/{endpoint}", params=params,
                                 headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()

    def _login(self):
        """Log in with an app password and read through the PDS. False if no
        password is configured."""
        password = (self.env.get("BLUESKY_APP_PASSWORD") or "").strip()
        if not password:
            return False
        pds = (self.env.get("BLUESKY_PDS_URL") or "").strip().rstrip("/") or DEFAULT_PDS
        response = self.http.post(f"{pds}/xrpc/com.atproto.server.createSession", timeout=TIMEOUT,
                                  json={"identifier": self.handle, "password": password})
        response.raise_for_status()
        self.token = response.json()["accessJwt"]
        self.api_base = f"{pds}/xrpc"
        return True

    def _fetch(self):
        params_profile = {"actor": self.handle}
        params_feed = {"actor": self.handle, "limit": FEED_LIMIT}
        try:
            return (self._get("app.bsky.actor.getProfile", params_profile),
                    self._get("app.bsky.feed.getAuthorFeed", params_feed),
                    "public")
        except requests.RequestException:
            if not self._login():
                raise
            return (self._get("app.bsky.actor.getProfile", params_profile),
                    self._get("app.bsky.feed.getAuthorFeed", params_feed),
                    "app password")

    # ---------- snapshot ----------

    def collect(self):
        profile, feed, read_via = self._fetch()
        items = [_feed_item(entry) for entry in feed.get("feed", [])]
        items = [i for i in items if i]

        own_dates = [i["date"] for i in items if i["kind"] != "repost" and i["date"]]
        last = max(own_dates) if own_dates else None
        stats = {
            "followers": profile.get("followersCount", 0),
            "posts_total": profile.get("postsCount", 0),
            "entries_read": len(items),
            "reposts": sum(1 for i in items if i["kind"] == "repost"),
            "last_activity": last,
            "days_since_activity": _days_since(last),
            "read_via": read_via,
        }
        identity = {
            "display_name": profile.get("displayName"),
            "bio": profile.get("description"),
        }
        return make_snapshot("bluesky", f"bluesky:{self.handle}", identity, items, stats)


def _feed_item(entry):
    post = entry.get("post") or {}
    record = post.get("record") or {}
    uri = post.get("uri")
    if not uri:
        return None
    author = (post.get("author") or {}).get("handle", "")
    reason = entry.get("reason") or {}
    is_repost = reason.get("$type", "").endswith("reasonRepost")

    if is_repost:
        kind, weight = "repost", REPOST_WEIGHT
        date = (reason.get("indexedAt") or "")[:10] or None
    else:
        kind = "reply" if record.get("reply") else "post"
        weight = 1.0
        date = (record.get("createdAt") or "")[:10] or None

    rkey = uri.rsplit("/", 1)[-1]
    return {
        "kind": kind,
        "id": f"repost:{uri}" if is_repost else uri,
        "date": date,
        "title": "",
        "text": record.get("text") or "",
        "url": f"https://bsky.app/profile/{author}/post/{rkey}" if author else None,
        "weight": weight,
        "meta": {"author": author} if is_repost else {},
        "signals": {
            "likes": post.get("likeCount", 0),
            "reposts": post.get("repostCount", 0),
            "replies": post.get("replyCount", 0),
            "quotes": post.get("quoteCount", 0),
        },
    }


def _days_since(date):
    if not date:
        return None
    try:
        then = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).days


if __name__ == "__main__":
    import json
    import sys
    snap = BlueSkyScanner(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BLUESKY_HANDLE", ""),
                          Path(__file__).resolve().parent.parent).collect()
    print(json.dumps({k: v for k, v in snap.items() if k != "items"}, indent=2))
    print(f"{len(snap['items'])} feed entries")

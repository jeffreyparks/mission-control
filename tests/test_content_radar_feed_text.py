"""Content Radar uses a feed's full article text instead of fetching the page.

Medium-hosted blogs (Netflix, Airbnb, Booking) 403 scripted page fetches but
carry the whole article in the feed's content:encoded. That text must be kept
(not cut to a short excerpt) and used without a fetch. A short teaser must
still trigger a fetch, and remain the fallback when the fetch fails.
"""
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import content_radar as cr_mod
from content_radar import ContentRadar

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

# ---------------- collect_entries keeps the feed's full text ----------------
recent = format_datetime(datetime.now(timezone.utc) - timedelta(days=1))
full_body = "<p>" + "Kafka consumer capacity testing in practice. " * 400 + "</p>"
teaser = "A short teaser about workload attestation on managed compute. " * 5
feed = f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Test</title>
<item><title>Full post</title><link>https://example.com/full</link>
  <pubDate>{recent}</pubDate><description>Short blurb.</description>
  <content:encoded><![CDATA[{full_body}]]></content:encoded></item>
<item><title>Teaser post</title><link>https://example.com/teaser</link>
  <pubDate>{recent}</pubDate><description>{teaser}</description></item>
</channel></rss>"""

radar = ContentRadar.__new__(ContentRadar)   # skip __init__: no workspace, no LLM
entries, considered, live = radar.collect_entries([{"name": "Test", "url": feed}])
by_title = {e["title"]: e for e in entries}
full, short = by_title.get("Full post"), by_title.get("Teaser post")
check("both in-window entries collected", full is not None and short is not None,
      str(list(by_title)))
check("full post keeps content:encoded, not the 800-char excerpt",
      full and len(full["summary"]) > 800, str(full and len(full["summary"])))
check("feed text is capped at MAX_ARTICLE_CHARS",
      full and len(full["summary"]) <= cr_mod.MAX_ARTICLE_CHARS)
check("feed text is plain text, not HTML", full and "<p>" not in full["summary"])
check("teaser falls back to the summary",
      short and short["summary"].startswith("A short teaser"))

# ---------------- hydrate skips the fetch only for full text ----------------
fetched = []
def fake_extract(url, page_text=""):
    fetched.append(url)
    return page_text

radar.extract_text = fake_extract
out = radar.hydrate([dict(full), dict(short)])
check("full-text post is not fetched", "https://example.com/full" not in fetched, str(fetched))
check("teaser post is fetched", "https://example.com/teaser" in fetched, str(fetched))
texts = {e["title"]: e["text"] for e in out}
check("full-text post uses the feed text", texts.get("Full post") == full["summary"])
check("failed teaser fetch falls back to the teaser text",
      texts.get("Teaser post") == short["summary"], str(list(texts)))

if fails:
    print(f"\n{len(fails)} failed")
    sys.exit(1)
print("\nall passed")

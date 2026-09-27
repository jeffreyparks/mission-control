"""
Site collector: a personal or portfolio site, as one snapshot.

Sites are listed in config/profile.yaml (per workspace):

    sites:
      - url: https://you.github.io/
        max_pages: 0        # the root only; N = the root plus N more from sitemap.xml

Captured:
  identity  page title, meta description, og:title, the first h1, and the hero -
            the masthead text a visitor reads before anything else
  items     one "section" per h2 on each page (text before the first h2 is an
            "intro" section), so even a one-pager gives an evaluator separate
            pieces to quote; plus one "page" item per page holding its outbound
            links, for checking that the site and the other profiles agree
  stats     pages fetched, characters kept, whether the text cap cut anything

robots.txt is honoured: a site that disallows this collector is skipped. Some
static-site generators wrap robots.txt in the page layout; the rules are read
out of the HTML so such a site is not mistaken for one with no rules.
"""
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from profile_snapshot import make_snapshot

USER_AGENT = "MissionControl-ProfileCheck/1.0 (reads your own public site)"
TIMEOUT = 20
MAX_TEXT_CHARS = 200_000
HERO_CHARS = 600
MAX_LINKS = 50
STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "nav", "footer", "form")
BLOCK_TAGS = ("p", "div", "li", "tr", "br", "section", "article", "header", "blockquote",
              "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "pre", "figcaption")
# Inline tags that sites use as separate chips ("12 yrs" "$40M budget"):
# a space after each keeps their words apart.
SPACED_TAGS = ("span", "a", "time", "small")


class SiteScanner:
    def __init__(self, url, base_dir, max_pages=0, session=None):
        self.url = normalise_url(url)
        self.base_dir = Path(base_dir)
        self.max_pages = max(0, int(max_pages or 0))
        self.http = session or requests.Session()
        self.host = urlparse(self.url).netloc
        self.robots = None

    # ---------- transport ----------

    def _get(self, url):
        response = self.http.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                                 allow_redirects=True)
        response.raise_for_status()
        return response

    def _load_robots(self):
        """Parse robots.txt; a missing or unreadable one allows everything."""
        parser = RobotFileParser()
        try:
            response = self._get(urljoin(self.url, "/robots.txt"))
        except requests.RequestException:
            parser.parse([])
            return parser
        text = response.text or ""
        if "html" in response.headers.get("content-type", "") or "<html" in text[:500].lower():
            text = BeautifulSoup(text, "lxml").get_text("\n")
        parser.parse(text.splitlines())
        return parser

    def _allowed(self, url):
        return self.robots.can_fetch(USER_AGENT, url)

    def _sitemap_pages(self):
        """Up to max_pages same-host URLs from sitemap.xml, root excluded."""
        if not self.max_pages:
            return []
        try:
            xml = self._get(urljoin(self.url, "/sitemap.xml")).text
        except requests.RequestException:
            return []
        out = []
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
            page = normalise_url(loc)
            if urlparse(page).netloc == self.host and page != self.url and page not in out:
                out.append(page)
        return out[: self.max_pages]

    # ---------- snapshot ----------

    def collect(self):
        self.robots = self._load_robots()
        if not self._allowed(self.url):
            print(f"   site {self.url}: robots.txt disallows this collector - skipped")
            return None

        pages = [self.url] + [p for p in self._sitemap_pages() if self._allowed(p)]
        identity, items = {}, []
        budget, truncated, fetched = MAX_TEXT_CHARS, False, 0

        for url in pages:
            html = self._get(url).text
            fetched += 1
            page = parse_page(html, url)
            if url == self.url:
                identity = page["identity"]
            items.append({
                "kind": "page",
                "id": url,
                "title": page["identity"].get("title", ""),
                "url": url,
                "meta": {"links": page["links"]},
            })
            for section in page["sections"]:
                text = section["text"]
                if len(text) > budget:
                    text, truncated = text[:budget], True
                budget -= len(text)
                if not text and not section["title"]:
                    continue
                items.append({
                    "kind": "section",
                    "id": f"{url}#{section['slug']}",
                    "title": section["title"],
                    "text": text,
                    "url": url,
                })
            if budget <= 0:
                truncated = True
                break

        stats = {
            "pages_fetched": fetched,
            "chars": MAX_TEXT_CHARS - budget,
            "truncated": truncated,
            "sections": sum(1 for i in items if i["kind"] == "section"),
        }
        return make_snapshot("site", f"site:{self.url}", identity, items, stats)


# ---------- parsing ----------

def normalise_url(url):
    """https://x.dev -> https://x.dev/ ; fragments dropped; path kept."""
    parts = urlparse(url.strip())
    path = parts.path or "/"
    return f"{parts.scheme or 'https'}://{parts.netloc}{path}" + (f"?{parts.query}" if parts.query else "")


def _clean(text):
    lines = [re.sub(r" ([.,;:!?)])", r"\1", re.sub(r"[ \t\u00a0]+", " ", line)).strip()
             for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def _slug(text, seen):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
    slug, n = base, 2
    while slug in seen:
        slug, n = f"{base}-{n}", n + 1
    seen.add(slug)
    return slug


def parse_page(html, url):
    """identity fields, external links, and h2 sections of one HTML page."""
    soup = BeautifulSoup(html, "lxml")

    def meta(**attrs):
        tag = soup.find("meta", attrs=attrs)
        return (tag.get("content") or "").strip() if tag else ""

    h1 = soup.find("h1")
    identity = {
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "description": meta(name="description"),
        "og_title": meta(property="og:title"),
        "h1": h1.get_text(" ", strip=True) if h1 else "",
    }

    host = urlparse(url).netloc
    links = sorted({a["href"].split("#")[0] for a in soup.find_all("a", href=True)
                    if a["href"].startswith("http") and urlparse(a["href"]).netloc != host})

    body = soup.body or soup
    for tag in body.find_all(STRIP_TAGS):
        tag.decompose()
    for comment in body.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in body.find_all(BLOCK_TAGS):
        tag.append("\n")
    for tag in body.find_all(SPACED_TAGS):
        tag.append(" ")

    masthead = body.find("header")
    if masthead is not None:
        if h1 is not None and masthead.find("h1") is not None:
            masthead.find("h1").decompose()
        identity["hero"] = _clean(masthead.get_text())[:HERO_CHARS]
        masthead.decompose()
    elif h1 is not None:
        h1.decompose()

    seen = set()
    sections = [{"slug": _slug("intro", seen), "title": "", "parts": []}]
    for node in body.descendants:
        if isinstance(node, Tag) and node.name == "h2":
            title = node.get_text(" ", strip=True)
            sections.append({"slug": _slug(title, seen), "title": title, "parts": []})
        elif isinstance(node, NavigableString) and node.find_parent("h2") is None:
            if node.find_parent("h1") is not None:
                continue
            sections[-1]["parts"].append(str(node))

    out = []
    for section in sections:
        text = _clean("".join(section["parts"]))
        if text or section["title"]:
            out.append({"slug": section["slug"], "title": section["title"], "text": text})
    return {"identity": identity, "links": links[:MAX_LINKS], "sections": out}


if __name__ == "__main__":
    import json
    import sys
    snap = SiteScanner(sys.argv[1], Path(__file__).resolve().parent.parent,
                       max_pages=int(sys.argv[2]) if len(sys.argv) > 2 else 0).collect()
    if snap:
        print(json.dumps({k: v for k, v in snap.items() if k != "items"}, indent=2))
        for item in snap["items"]:
            print(f"- {item['kind']:8s} {item['title']!r}: {len(item['text'])} chars")

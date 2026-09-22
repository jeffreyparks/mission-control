#!/usr/bin/env python3
"""
Content Radar Agent (WEEKLY)
============================

Finds the few pieces of content this week that the candidate can actually say
something new about, and states plainly when there is nothing.

Pipeline
--------
1. Read RSS sources from ``config/content-sources.yaml`` (config contract kept:
   a top-level ``sources:`` list of ``{name, url, category}``).
2. Keep only entries published in the last ``MAX_AGE_DAYS`` (14) days.
3. Fetch each candidate article and extract the FULL body text with
   requests + BeautifulSoup (nav/script/style/footer stripped), capped at
   ``MAX_ARTICLE_CHARS`` (6000) characters. RSS ``<summary>`` is a fallback only.
4. One batched LLM call judges every article together and writes sections
   00 / 01 / 02. A second batched LLM call judges the GitHub repos and writes
   sections 03 / 04.
5. Emit ``artifacts/content/radar-YYYY-MM-DD.json`` (machine, for the HTML
   renderer) and ``artifacts/content/radar-YYYY-MM-DD.md`` (human mirror).

Empty is a valid answer. The prompts explicitly permit "nothing this week",
"no change", and "skip this one". Under-claiming is rewarded; padding is not.

JSON SCHEMA (artifacts/content/radar-YYYY-MM-DD.json)
-----------------------------------------------------
{
  "agent": "content_radar",
  "cadence": "weekly",
  "date": "YYYY-MM-DD",              # run date
  "window_days": 14,                 # article recency window
  "watch_topics": [str],             # profile.md "## Watch Topics", in order
  "stats": {
    "sources_configured": int,
    "sources_with_entries": int,
    "articles_considered": int,      # in-window entries seen
    "articles_fetched": int,         # full text successfully extracted
    "picks": int,
    "themes": [str],                 # distinct themes the picks covered
    "repos_reviewed": int,
    "llm": str                       # LLM cost/cache report line
  },
  "sections": {
    "00_filter": {
      "title": "The filter every pick ran through",
      "text": str                    # one short paragraph, LLM-written
    },
    "01_pillar_picks": {
      "title": "Pillar picks",
      "note": str,                   # optional editor note ("" if none)
      "picks": [
        {
          "tier": "primary" | "secondary" | "supporting",
          "theme": str,              # e.g. "Evaluation Rigor"
          "watch_topic": str,        # matched "## Watch Topics" entry, "" if none
          "headline": str,           # rewritten, not the source title
          "why_it_matters": str,     # 2-4 sentences of analysis
          "angle": str,              # the POV the candidate could own
          "formats": [str],          # subset of FORMATS
          "source_name": str,
          "source_url": str,
          "source_date": str         # YYYY-MM-DD or ""
        }
      ]
    },
    "02_network_signal": {
      "title": "Network signal",
      "summary": str,                # plain-language verdict, may say "no signal"
      "rows": [
        {"label": str, "note": str, "chips": [str]}
      ]
    },
    "03_repo_signal": {
      "title": "Repo signal",
      "summary": str,
      "repos": [
        {"name": str, "url": str, "verdict": "light-touch" | "no-change",
         "reason": str}
      ]
    },
    "04_queue": {
      "title": "The queue",
      "items": [str]                 # 3-5 short checklist items ("" list if dead week)
    }
  },
  "raw_articles": [                  # provenance for the picks, no scores
    {"source": str, "category": str, "title": str, "url": str,
     "published": "YYYY-MM-DD", "chars": int}
  ]
}
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import LLM, LLMError          # noqa: E402
from context import context_block      # noqa: E402
from profile_keywords import load_watch_topics   # noqa: E402

MAX_AGE_DAYS = 14
MAX_ARTICLE_CHARS = 6000
MAX_ENTRIES_PER_SOURCE = 6
MAX_ARTICLES_TOTAL = 30
TARGET_PICKS = 8
MAX_PICKS_PER_THEME = 3
FORMATS = ["LinkedIn post", "Tweet", "Short technical post", "LinkedIn comment + reply"]
TIERS = ["primary", "secondary", "supporting"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; MissionControl/1.0; +content-radar)"}



def _config_path(base_dir, filename):
    """Per-workspace config when present, else the repo's tracked default, so a
    new workspace works with no setup but can still override."""
    from pathlib import Path as _P
    local = _P(base_dir) / "config" / filename
    if local.exists():
        return local
    return _P(__file__).resolve().parent.parent / "config" / filename

class ContentRadar:
    def __init__(self, base_dir, github_user=None):
        self.base_dir = Path(base_dir)
        self.sources_path = _config_path(self.base_dir, "content-sources.yaml")
        self.out_dir = self.base_dir / "artifacts/content"
        # Whose repos to read. Comes from the workspace's own .env via the
        # caller, so the radar and the profile scanner can never disagree about
        # which person this run is for. Empty -> the GitHub section is skipped.
        self.github_user = (github_user or "").strip()
        # Emerging themes from me/profile.md "## Watch Topics". Empty is fine:
        # the radar then judges on the profile alone, exactly as before.
        self.watch_topics = load_watch_topics(self.base_dir)
        self.llm = LLM(self.base_dir)

    @staticmethod
    def _as_payload(data, list_key):
        """Normalize one LLM JSON result into the dict shape this agent reads.

        `LLM._extract_json` is documented to return "the first JSON object OR
        ARRAY", so a model that answers with a bare `[...]` of picks instead of
        the requested `{"picks": [...]}` wrapper is a valid parse that this
        agent used to crash on ('list' object has no attribute 'get') - and
        because the parse succeeded, the bad shape was cached and the crash
        repeated on every later run.

        A bare list is taken at face value as the list the prompt asked for,
        which is what the model meant. Anything else degrades to {}, so the
        section renders its honest empty state instead of killing the run.
        """
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {list_key: [d for d in data if isinstance(d, dict)]}
        return {}

    @staticmethod
    def _wants_object(data):
        """Validator for complete_json: insist on the documented wrapper.

        Passed as `validate=`, so a bare array costs a stricter retry and an
        escalation up the model ladder BEFORE `_as_payload` has to salvage it.
        """
        return isinstance(data, dict)

    def watch_block(self):
        """The watch-topic instruction injected into the article prompt.

        A resume describes what the candidate HAS done, so a prompt anchored
        only on the resume keeps selecting the same few themes forever. Watch
        topics are the counterweight: explicit permission to pick an article
        about where the field is going, with no track record required."""
        if not self.watch_topics:
            return ""
        lines = [
            f"- {t['topic']}" + (f" - {t['note']}" if t.get("note") else "")
            for t in self.watch_topics
        ]
        return (
            "\nACTIVE WATCH TOPICS (from profile.md '## Watch Topics')\n"
            "These are emerging themes the candidate is deliberately tracking.\n"
            "He does NOT need existing experience in them to have a view; an\n"
            "informed practitioner reading in public is a legitimate angle.\n"
            "Treat a strong article on one of these as pick-worthy on its own\n"
            "merits, and name the topic in the pick's watch_topic field.\n"
            + "\n".join(lines) + "\n"
        )

    # ---------------- config ----------------

    def load_sources(self):
        """Load content sources from YAML config. Contract: top-level `sources:`."""
        if not self.sources_path.exists():
            print(f"[warn] no content sources at {self.sources_path}")
            return []
        config = yaml.safe_load(self.sources_path.read_text()) or {}
        sources = config.get("sources") or []
        return [s for s in sources if isinstance(s, dict) and s.get("url")]

    # ---------------- fetching ----------------

    @staticmethod
    def _entry_date(entry):
        for key in ("published_parsed", "updated_parsed"):
            parsed = entry.get(key)
            if parsed:
                try:
                    return datetime(*parsed[:6], tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
        return None

    def collect_entries(self, sources):
        """Return in-window feed entries, newest first, spread across sources."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
        per_source, considered, live_sources = {}, 0, 0

        for source in sources:
            name = source.get("name", source["url"])
            try:
                feed = feedparser.parse(source["url"])
            except Exception as exc:                      # noqa: BLE001
                print(f"  x {name}: {exc}")
                continue
            if not feed.entries:
                print(f"  - {name}: no entries")
                continue
            live_sources += 1

            picked = []
            for entry in feed.entries[:25]:
                published = self._entry_date(entry)
                if published is None or published < cutoff:
                    continue
                considered += 1
                picked.append({
                    "source": name,
                    "category": source.get("category", "Uncategorized"),
                    "title": (entry.get("title") or "Untitled").strip(),
                    "url": entry.get("link") or "",
                    "published": published,
                    "summary": BeautifulSoup(
                        entry.get("summary", "") or "", "html.parser"
                    ).get_text(" ", strip=True)[:800],
                })
                if len(picked) >= MAX_ENTRIES_PER_SOURCE:
                    break
            per_source[name] = picked
            print(f"  + {name}: {len(picked)} in-window")

        # round-robin so one prolific feed cannot crowd out the rest
        ordered, index = [], 0
        while len(ordered) < MAX_ARTICLES_TOTAL:
            added = False
            for items in per_source.values():
                if index < len(items):
                    ordered.append(items[index])
                    added = True
                    if len(ordered) >= MAX_ARTICLES_TOTAL:
                        break
            if not added:
                break
            index += 1
        return ordered, considered, live_sources

    @staticmethod
    def extract_text(url):
        """Fetch an article and return clean body text (<= MAX_ARTICLE_CHARS)."""
        if not url:
            return ""
        try:
            resp = requests.get(url, timeout=20, headers=UA)
            resp.raise_for_status()
        except Exception as exc:                          # noqa: BLE001
            print(f"    x fetch failed: {exc}")
            return ""
        if "html" not in resp.headers.get("Content-Type", "text/html"):
            return ""

        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                         "form", "noscript", "iframe", "svg"]):
            tag.decompose()
        body = soup.find("article") or soup.find("main") or soup.body or soup
        text = body.get_text("\n", strip=True)
        text = re.sub(r"\n{2,}", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text[:MAX_ARTICLE_CHARS]

    def hydrate(self, entries):
        """Attach full article text; drop entries with no usable body."""
        out = []
        for entry in entries:
            print(f"  . fetching: {entry['title'][:70]}")
            text = self.extract_text(entry["url"])
            if len(text) < 400:
                text = entry["summary"]
            if len(text) < 200:
                continue
            entry["text"] = text
            out.append(entry)
        return out

    def fetch_repos(self):
        """Public repos for the candidate's GitHub user. No user configured for
        this workspace means no GitHub section, not a request to /users//repos."""
        if not self.github_user:
            return []
        url = f"https://api.github.com/users/{self.github_user}/repos"
        try:
            resp = requests.get(url, timeout=20, headers=UA,
                                params={"per_page": 100, "sort": "updated"})
            resp.raise_for_status()
            repos = resp.json()
        except Exception as exc:                          # noqa: BLE001
            print(f"  x github: {exc}")
            return []
        return [{
            "name": r.get("name", ""),
            "url": r.get("html_url", ""),
            "description": (r.get("description") or "")[:300],
            "language": r.get("language") or "",
            "topics": r.get("topics") or [],
            "pushed_at": (r.get("pushed_at") or "")[:10],
            "stars": r.get("stargazers_count", 0),
            "archived": bool(r.get("archived")),
        } for r in repos if isinstance(r, dict) and not r.get("fork")]

    # ---------------- LLM analysis ----------------

    def analyze_articles(self, articles, run_date):
        """ONE batched call over every article -> sections 00, 01, 02."""
        if not articles:
            return None

        blocks = []
        for i, a in enumerate(articles, 1):
            blocks.append(
                f"--- ARTICLE {i} ---\n"
                f"source_name: {a['source']} ({a['category']})\n"
                f"source_url: {a['url']}\n"
                f"source_date: {a['published'].strftime('%Y-%m-%d')}\n"
                f"original_title: {a['title']}\n"
                f"full_text:\n{a['text']}\n"
            )

        watch_block = self.watch_block()
        prompt = f"""{context_block(self.base_dir)}

=== TASK: WEEKLY CONTENT RADAR (editorial judgment, not keyword matching) ===
Today is {run_date}. Below are {len(articles)} articles published in the last
{MAX_AGE_DAYS} days, with their full body text.

You are the candidate's editorial strategist. Decide which of these, if any,
give this specific person a reason to publish something that is actually his.

HARD RULES ON HONESTY
- Most articles are not worth a post. Say so by leaving them out.
- {TARGET_PICKS} picks is the target, not a ceiling to stop at early. Review every
  candidate article before deciding you are done - do not settle for 2 or 3
  strong ones without checking whether the rest of the batch has more that
  genuinely clear the bar. That said, fewer is still better than padded, and
  ZERO picks is a fully acceptable and correct answer when the week is
  genuinely dead.
- Never invent a connection to the candidate's background. If the link is
  thin, skip it. Do not force it.
- Never restate a headline as "insight". If you cannot state a specific angle
  the candidate could defend in a comment thread, drop the pick.
- Do not pad "why_it_matters" with generic industry commentary.

RULES ON BREADTH (read profile.md's two pillars before you judge)
- Causal measurement is ONE of this candidate's pillars, not an entry
  requirement. Applied AI, agentic systems, evaluation, AI product and
  platform delivery, and AI in advertising/GTM are first-class subjects in
  their own right. A strong article there needs NO causal or marketing-
  measurement hook to qualify.
- Do NOT bend an AI or engineering article into a measurement story just to
  connect it to the resume. If the honest angle is an applied-AI angle, say
  the applied-AI angle.
- No more than {MAX_PICKS_PER_THEME} picks may share a theme. If your picks are
  collapsing onto one theme, you are pattern-matching the resume instead of
  reading the week - go back and look at what you skipped.
- Aim for a spread across at least 3 distinct themes when the batch supports
  it, and prefer a pick that opens a NEW line of authority over a fourth pick
  restating an established one.
{watch_block}
Return ONLY a JSON object with this exact shape:
{{
  "filter": "one short paragraph (2-4 sentences), first person plural or neutral, stating the judgment criterion you applied to THIS week's set. Be concrete about what you rejected and why.",
  "picks": [
    {{
      "tier": one of {TIERS},
      "theme": "short label, e.g. Evaluation Rigor, Measurement Craft, Agentic Systems, Applied AI in GTM, Platform Delivery, Build in Public, Curated Commentary",
      "watch_topic": "the ACTIVE WATCH TOPIC this pick serves, copied exactly, or \"\" if it serves none",
      "headline": "rewritten, punchy, 4-12 words. NOT the source title.",
      "why_it_matters": "2-4 sentences of real analysis tied to THIS candidate's positioning - EITHER pillar. Name the specific pillar you are drawing on instead of defaulting to causal measurement.",
      "angle": "the single specific point of view the candidate could own here. Most valuable field. Be opinionated and narrow.",
      "formats": subset of {FORMATS},
      "source_name": "exact source_name from the article block",
      "source_url": "exact source_url",
      "source_date": "exact source_date"
    }}
  ],
  "network_summary": "plain language: do these picks give him a real reason to engage with anyone? If not, say 'No network signal this week.' and stop.",
  "network_rows": [
    {{"label": "e.g. Warm doors / Field peers / Cold but relevant",
      "note": "who and why, concretely, tied to the picks",
      "chips": ["optional short tags"]}}
  ]
}}
Use at most one "primary" tier pick. Empty arrays are allowed.
No markdown, no commentary outside the JSON.

{chr(10).join(blocks)}
"""
        try:
            return self._as_payload(
                self.llm.complete_json(prompt, tag="content-radar-articles",
                                       validate=self._wants_object),
                "picks")
        except LLMError as exc:
            print(f"  x LLM article analysis failed: {exc}")
            return None

    def analyze_repos(self, repos, picks, filter_text, run_date):
        """ONE batched call over every repo -> sections 03, 04."""
        repo_lines = [
            f"- name: {r['name']} | url: {r['url']} | lang: {r['language']} | "
            f"stars: {r['stars']} | last_push: {r['pushed_at']} | "
            f"topics: {', '.join(r['topics']) or 'none'} | archived: {r['archived']} | "
            f"description: {r['description'] or 'none'}"
            for r in repos
        ]
        pick_lines = [
            f"- [{p.get('tier')}] {p.get('theme')}: {p.get('headline')} :: angle: {p.get('angle')}"
            for p in picks
        ] or ["- (no picks this week)"]

        prompt = f"""{context_block(self.base_dir)}

=== TASK: WEEKLY REPO SIGNAL + ACTION QUEUE ===
Today is {run_date}.

This week's editorial filter:
{filter_text or '(no picks were made this week)'}

This week's content picks:
{chr(10).join(pick_lines)}

The candidate's public GitHub repos (user {self.github_user}):
{chr(10).join(repo_lines) or '- (none found)'}

PART A - repo signal. For EVERY repo above return a verdict:
  "light-touch" = there is a real, small, this-week reason to touch it
                  (a README line, a topic, a tiny example) that is directly
                  connected to this week's picks or to an obvious positioning gap.
  "no-change"   = leave it alone.
MOST WEEKS MOST REPOS ARE "no-change". That is the correct and expected result.
Do not invent busywork. At most 2 repos may be "light-touch"; zero is fine.
Each repo gets a one-line reason (<= 20 words).

PART B - the queue. 3 to 5 short checklist items the candidate can actually do
this week, derived from the picks and repo verdicts. Imperative voice, under 15
words each. If the week is dead, return 1-2 honest maintenance items or an
empty list. Never pad to hit five.

Return ONLY JSON:
{{
  "repo_summary": "one honest sentence about the state of the repos this week",
  "repos": [{{"name": "...", "url": "...", "verdict": "light-touch" | "no-change", "reason": "..."}}],
  "queue": ["...", "..."]
}}
No markdown, no commentary outside the JSON.
"""
        try:
            return self._as_payload(
                self.llm.complete_json(prompt, tag="content-radar-repos",
                                       validate=self._wants_object),
                "repos")
        except LLMError as exc:
            print(f"  x LLM repo analysis failed: {exc}")
            return None

    # ---------------- assembly ----------------

    @staticmethod
    def _clean_picks(raw_picks, articles, watch_topics=()):
        """Validate the model's picks and enforce breadth in code.

        The prompt asks for a spread of themes; this is the part that holds
        when the model ignores it. A theme that has already filled
        MAX_PICKS_PER_THEME slots is dropped rather than silently allowed to
        take over the week - the failure mode this radar had was six picks
        that were all one pillar."""
        valid_urls = {a["url"] for a in articles}
        known_topics = {str(t.get("topic", "")).lower(): t.get("topic", "")
                        for t in (watch_topics or [])}
        picks, primaries, per_theme = [], 0, {}
        for p in raw_picks or []:
            if not isinstance(p, dict):
                continue
            url = (p.get("source_url") or "").strip()
            if url and valid_urls and url not in valid_urls:
                continue  # no hallucinated sources
            tier = p.get("tier") if p.get("tier") in TIERS else "supporting"
            if tier == "primary":
                primaries += 1
                if primaries > 1:
                    tier = "secondary"
            formats = [f for f in (p.get("formats") or []) if f in FORMATS]
            theme = (p.get("theme") or "").strip()
            theme_key = theme.lower()
            if theme_key and per_theme.get(theme_key, 0) >= MAX_PICKS_PER_THEME:
                continue
            per_theme[theme_key] = per_theme.get(theme_key, 0) + 1
            picks.append({
                "tier": tier,
                "theme": theme,
                "watch_topic": known_topics.get(
                    (p.get("watch_topic") or "").strip().lower(), ""),
                "headline": (p.get("headline") or "").strip(),
                "why_it_matters": (p.get("why_it_matters") or "").strip(),
                "angle": (p.get("angle") or "").strip(),
                "formats": formats or ["LinkedIn post"],
                "source_name": (p.get("source_name") or "").strip(),
                "source_url": url,
                "source_date": (p.get("source_date") or "").strip(),
            })
        order = {t: i for i, t in enumerate(TIERS)}
        picks.sort(key=lambda p: order.get(p["tier"], 9))
        return picks

    def build_payload(self, run_date, articles, article_result, repo_result,
                      repos, stats):
        # Defensive: build_payload is also reachable from tests and future
        # callers, so it normalizes rather than trusting its inputs.
        article_result = self._as_payload(article_result or {}, "picks")
        repo_result = self._as_payload(repo_result or {}, "repos")
        picks = self._clean_picks(article_result.get("picks"), articles,
                                  self.watch_topics)

        rows = []
        for row in article_result.get("network_rows") or []:
            if isinstance(row, dict) and (row.get("label") or row.get("note")):
                rows.append({
                    "label": (row.get("label") or "").strip(),
                    "note": (row.get("note") or "").strip(),
                    "chips": [str(c) for c in (row.get("chips") or [])],
                })

        known = {r["name"]: r["url"] for r in repos}
        repo_rows, light = [], 0
        for r in repo_result.get("repos") or []:
            if not isinstance(r, dict) or r.get("name") not in known:
                continue
            verdict = "light-touch" if r.get("verdict") == "light-touch" else "no-change"
            if verdict == "light-touch":
                light += 1
                if light > 2:
                    verdict = "no-change"
            repo_rows.append({
                "name": r["name"],
                "url": known[r["name"]],
                "verdict": verdict,
                "reason": (r.get("reason") or "").strip(),
            })
        seen = {r["name"] for r in repo_rows}
        for r in repos:
            if r["name"] not in seen:
                repo_rows.append({"name": r["name"], "url": r["url"],
                                  "verdict": "no-change", "reason": "Not flagged this week."})
        repo_rows.sort(key=lambda r: (r["verdict"] != "light-touch", r["name"].lower()))

        queue = [str(q).strip() for q in (repo_result.get("queue") or []) if str(q).strip()][:5]

        no_picks_note = "" if picks else "No pick cleared the bar this week. Nothing to publish; that is the result."

        return {
            "agent": "content_radar",
            "cadence": "weekly",
            "date": run_date,
            "window_days": MAX_AGE_DAYS,
            "watch_topics": [t["topic"] for t in self.watch_topics],
            "stats": {**stats, "picks": len(picks), "repos_reviewed": len(repo_rows),
                      "themes": sorted({p["theme"] for p in picks if p["theme"]}),
                      "llm": self.llm.report()},
            "sections": {
                "00_filter": {
                    "title": "The filter every pick ran through",
                    "text": (article_result.get("filter") or
                             "No articles cleared the recency and substance checks this week, so no filter was applied.").strip(),
                },
                "01_pillar_picks": {
                    "title": "Pillar picks",
                    "note": no_picks_note,
                    "picks": picks,
                },
                "02_network_signal": {
                    "title": "Network signal",
                    "summary": (article_result.get("network_summary") or
                                "No network signal this week.").strip(),
                    "rows": rows,
                },
                "03_repo_signal": {
                    "title": "Repo signal",
                    "summary": (repo_result.get("repo_summary") or
                                "Repos not reviewed this week.").strip(),
                    "repos": repo_rows,
                },
                "04_queue": {"title": "The queue", "items": queue},
            },
            "raw_articles": [{
                "source": a["source"],
                "category": a["category"],
                "title": a["title"],
                "url": a["url"],
                "published": a["published"].strftime("%Y-%m-%d"),
                "chars": len(a["text"]),
            } for a in articles],
        }

    # ---------------- rendering ----------------

    @staticmethod
    def render_markdown(payload):
        s = payload["sections"]
        st = payload["stats"]
        out = [
            f"# Content Radar - {payload['date']}",
            "",
            f"*Weekly. {st['articles_fetched']} articles read in full from "
            f"{st['sources_with_entries']}/{st['sources_configured']} live sources "
            f"({payload['window_days']}-day window). {st['picks']} picks.*",
            "",
            f"## 00 - {s['00_filter']['title']}",
            "",
            s["00_filter"]["text"],
            "",
            f"## 01 - {s['01_pillar_picks']['title']}",
            "",
        ]
        if s["01_pillar_picks"]["note"]:
            out += [f"> {s['01_pillar_picks']['note']}", ""]
        for i, p in enumerate(s["01_pillar_picks"]["picks"], 1):
            watch = f" | **Watching:** {p['watch_topic']}" if p.get("watch_topic") else ""
            out += [
                f"### {i}. {p['headline']}",
                f"**Tier:** {p['tier']} | **Theme:** {p['theme']}{watch}",
                "",
                f"**Why it matters.** {p['why_it_matters']}",
                "",
                f"**Angle.** {p['angle']}",
                "",
                f"**Formats:** {', '.join(p['formats'])}",
                f"**Source:** [{p['source_name']}]({p['source_url']}) - {p['source_date']}",
                "",
            ]
        out += [f"## 02 - {s['02_network_signal']['title']}", "",
                s["02_network_signal"]["summary"], ""]
        for row in s["02_network_signal"]["rows"]:
            chips = f" `{'` `'.join(row['chips'])}`" if row["chips"] else ""
            out.append(f"- **{row['label']}** - {row['note']}{chips}")
        if s["02_network_signal"]["rows"]:
            out.append("")

        out += [f"## 03 - {s['03_repo_signal']['title']}", "",
                s["03_repo_signal"]["summary"], ""]
        for r in s["03_repo_signal"]["repos"]:
            out.append(f"- `{r['name']}` - **{r['verdict']}** - {r['reason']}")
        out += ["", f"## 04 - {s['04_queue']['title']}", ""]
        if s["04_queue"]["items"]:
            out += [f"- [ ] {item}" for item in s["04_queue"]["items"]]
        else:
            out.append("- [ ] Nothing this week. Do not manufacture work.")
        out += ["", "---", f"*{st['llm']}*", ""]
        return "\n".join(out)

    # ---------------- entrypoint ----------------

    def run(self):
        run_date = datetime.now().strftime("%Y-%m-%d")
        sources = self.load_sources()
        print(f"Content Radar (weekly): {len(sources)} sources configured")

        entries, considered, live = self.collect_entries(sources)
        print(f"  -> {len(entries)} candidate articles, fetching full text")
        articles = self.hydrate(entries)
        print(f"  -> {len(articles)} articles with usable body text")

        article_result = self.analyze_articles(articles, run_date) or {}
        picks_preview = article_result.get("picks") or []

        repos = self.fetch_repos()
        print(f"  -> {len(repos)} public repos")
        repo_result = self.analyze_repos(
            repos, picks_preview, article_result.get("filter", ""), run_date
        ) if repos else None

        stats = {
            "sources_configured": len(sources),
            "sources_with_entries": live,
            "articles_considered": considered,
            "articles_fetched": len(articles),
        }
        payload = self.build_payload(run_date, articles, article_result,
                                     repo_result, repos, stats)

        self.out_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.out_dir / f"radar-{run_date}.json"
        md_path = self.out_dir / f"radar-{run_date}.md"
        json_path.write_text(json.dumps(payload, indent=2))
        md_path.write_text(self.render_markdown(payload))

        print(f"  -> {json_path}")
        print(f"  -> {md_path}")
        print(f"  {self.llm.report()}")
        return md_path


if __name__ == "__main__":
    ContentRadar(Path(__file__).resolve().parent.parent).run()

"""
Built In job board aggregator.

Built In runs one job-board platform across nine market hosts
(builtin.com plus per-city boards). Employers post directly to it; it is a
board-wide aggregator like Greenhouse/Lever/Ashby are per-company, not a
per-company API, so it has no single api_url and is configured separately
under job-sources.yaml's `aggregators:` list.

Ported from the reference parser at
  https://github.com/career-ops-hq/career-ops/blob/main/providers/builtin.mjs
which documents the page structure in detail; see that file for the full
rationale behind each design choice. This port keeps the same two-payload
approach:

  1. SPINE - the server-rendered schema.org ItemList blob, one entry per job:
     {"@type":"ListItem","position":N,"name":<title>,"url":<url>,"description":<~400ch>}
     This is the stable, REQUIRED half - title/url/description survive even if
     card parsing below breaks.

  2. ENRICHMENT - the rendered job card, joined to the spine by the numeric job
     id in the url. Fields are anchored on FontAwesome icon classes rather than
     field order, matching the reference parser's approach:
       fa-clock          -> posted badge ("Yesterday", "3 Days Ago")
       fa-house-building -> workplace mode (Remote / Hybrid / In-Office / ...)
       fa-location-dot   -> location, or "N Locations" + a tooltip
       fa-sack-dollar    -> salary band ("170K-230K Annually")
     plus the /company/ anchor immediately preceding the card title.

Every enriched field is optional; an unresolvable one is left empty rather than
guessed, matching the reference parser's stance that a wrong guess (especially
for location) is worse than an empty field.
"""
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

import requests

DEFAULT_HOST = "builtin.com"
DEFAULT_MAX_PAGES = 3
HARD_MAX_PAGES = 25

BROWSER_LIKE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# SSRF allowlist: accepted spellings -> canonical (redirect-free) host.
# builtinchicago is .org, not a typo.
HOSTS = {
    "builtin.com": "builtin.com", "www.builtin.com": "builtin.com",
    "builtinseattle.com": "www.builtinseattle.com", "www.builtinseattle.com": "www.builtinseattle.com",
    "builtinnyc.com": "www.builtinnyc.com", "www.builtinnyc.com": "www.builtinnyc.com",
    "builtinsf.com": "www.builtinsf.com", "www.builtinsf.com": "www.builtinsf.com",
    "builtinla.com": "www.builtinla.com", "www.builtinla.com": "www.builtinla.com",
    "builtinboston.com": "www.builtinboston.com", "www.builtinboston.com": "www.builtinboston.com",
    "builtinaustin.com": "www.builtinaustin.com", "www.builtinaustin.com": "www.builtinaustin.com",
    "builtinchicago.org": "www.builtinchicago.org", "www.builtinchicago.org": "www.builtinchicago.org",
    "builtincolorado.com": "www.builtincolorado.com", "www.builtincolorado.com": "www.builtincolorado.com",
}

# Drift-guard thresholds: loose on purpose. They exist to catch a LAYOUT
# CHANGE (near-total enrichment loss), not to grade a page whose jobs
# genuinely lack a field.
GUARD_MIN_ROWS = 5
GUARD_MIN_LOCATION = 0.5

ITEM = re.compile(
    r'\{"@type":"ListItem","position":\d+,"name":("(?:[^"\\]|\\.)*"),'
    r'"url":("(?:[^"\\]|\\.)*")(?:,"description":("(?:[^"\\]|\\.)*"))?\}'
)
CARD_ANCHOR = re.compile(r'data-id="job-card-title"')
CARD_ID = re.compile(r'data-builtin-track-job-id="(\d+)"')
COMPANY_ANCHOR = re.compile(r'<a[^>]+href="/company/[^"]*"[^>]*>([\s\S]{0,240}?)</a>')
LOCATION_TOOLTIP = re.compile(r'aria-label="Job locations"[^>]*data-bs-title="([^"]*)"')
CARD_CAP = 12_000  # chars; a card is ~3-6k, this bounds a missing next-anchor

WORKPLACE_MODES = ["Remote or Hybrid", "Remote", "Hybrid", "In-Office"]
MULTI_LOCATION = re.compile(r"^\d+\s+Locations?$", re.I)
SALARY_BAND = re.compile(r"^(\d+(?:\.\d+)?)K(?:-(\d+(?:\.\d+)?)K)?\s+Annually$", re.I)

_TAG = re.compile(r"<[^>]*>")
_WS = re.compile(r"\s+")


def _unescape(s):
    import html as _html
    return _html.unescape(s)


def strip_tags(s):
    return _WS.sub(" ", _unescape(_TAG.sub(" ", str(s)))).strip()


def resolve_host(raw):
    """Resolve a configured host spelling to the canonical host to request, or
    None when it is not one of the nine allowlisted Built In markets."""
    if raw is None or raw == "":
        return DEFAULT_HOST
    if not isinstance(raw, str):
        return None
    h = raw.strip().lower()
    if h == "":
        return DEFAULT_HOST
    if "/" in h:
        try:
            h = urlparse(h if "://" in h else f"https://{h}").hostname or ""
            h = h.lower()
        except ValueError:
            return None
    return HOSTS.get(h)


def assert_host(url):
    """SSRF guard: every request URL passes through here first. Re-checks the
    RESOLVED host against the allowlist rather than trusting the caller."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"builtin: URL must use HTTPS: {url}")
    host = (parsed.hostname or "").lower()
    if HOSTS.get(host) != host:
        raise ValueError(
            f'builtin: untrusted hostname "{parsed.hostname}" - must be one of '
            + ", ".join(sorted(set(HOSTS.values())))
        )
    return url


def field_after_icon(seg, icon):
    """Text of the first element following an icon marker inside a card.
    Anchoring on the icon class (rather than field order) survives a card
    layout reshuffle."""
    at = seg.find(icon)
    if at == -1:
        return ""
    window = seg[at:at + 600]
    for m in re.finditer(r">([^<>]+)<", window):
        t = strip_tags(m.group(1))
        if t:
            return t
    return ""


_RELATIVE = re.compile(
    r"^(\d+|an?)\+?\s+(minute|hour|day|week|month|year)s?\s+ago$", re.I
)
_UNIT_SECONDS = {
    "minute": 60, "hour": 3600, "day": 86400,
    "week": 604800, "month": 2592000, "year": 31536000,
}


def parse_posted_at(text, now=None):
    """Parse a posted-freshness badge ("2 Hours Ago", "Reposted 15 Days Ago",
    "Yesterday", "30+ Days Ago") into a datetime, or None when unrecognised."""
    if not isinstance(text, str):
        return None
    now = now or datetime.now()
    t = re.sub(r"^reposted\s+", "", text.strip(), flags=re.I)
    if re.match(r"^today$", t, re.I):
        return now
    if re.match(r"^yesterday$", t, re.I):
        return now - timedelta(days=1)
    m = _RELATIVE.match(t)
    if not m:
        return None
    n = 1 if re.match(r"^an?$", m.group(1), re.I) else int(m.group(1))
    seconds = _UNIT_SECONDS.get(m.group(2).lower())
    return now - timedelta(seconds=n * seconds) if seconds else None


def parse_salary(text):
    """Parse a card salary band into (min, max), annual USD.

    Only `Annually` bands are read - Built In also renders "115K-130K Hourly",
    which is the site's own data error (nobody bills $115k/hr); reading it
    would hand the pipeline a number three orders of magnitude off.
    """
    if not isinstance(text, str):
        return None
    m = SALARY_BAND.match(text.strip())
    if not m:
        return None
    lo = round(float(m.group(1)) * 1000)
    hi = round(float(m.group(2)) * 1000) if m.group(2) else lo
    return (lo, hi) if lo > 0 else None


def compose_location(mode, places):
    """Compose the location string. Returns '' whenever the card names no
    place - the SAFE direction, since an empty location is treated as
    unfiltered downstream rather than as a rejection."""
    places = [p for p in (places or []) if p]
    if not places:
        return mode if mode and "remote" in mode.lower() else ""
    return " · ".join([mode] + places) if mode else " · ".join(places)


def parse_cards(html, now=None):
    """Parse rendered job cards into {job_id: {company, location, salary, posted_at}}."""
    out = {}
    if not isinstance(html, str):
        return out
    starts = [m.start() for m in CARD_ANCHOR.finditer(html)]
    for i, start in enumerate(starts):
        end = min(starts[i + 1] if i + 1 < len(starts) else len(html), start + CARD_CAP)
        seg = html[start:end]
        idm = CARD_ID.search(seg)
        if not idm:
            continue
        job_id = idm.group(1)

        pre_from = max(0, start - 2500) if i == 0 else starts[i - 1]
        pre = html[pre_from:start]
        company = ""
        for cm in COMPANY_ANCHOR.finditer(pre):
            company = strip_tags(cm.group(1))  # last match in the window wins

        mode_raw = field_after_icon(seg, "fa-house-building")
        mode = next((w for w in WORKPLACE_MODES if w.lower() == mode_raw.lower()), "")

        loc_raw = field_after_icon(seg, "fa-location-dot")
        places = []
        if loc_raw and not MULTI_LOCATION.match(loc_raw):
            places = [loc_raw]
        else:
            tip = LOCATION_TOOLTIP.search(seg)
            if tip:
                places = [strip_tags(p) for p in re.split(r"</div>|<br\s*/?>", _unescape(tip.group(1)), flags=re.I)]
                places = [p for p in places if p]

        out[job_id] = {
            "company": company,
            "location": compose_location(mode, places),
            "salary": parse_salary(field_after_icon(seg, "fa-sack-dollar")),
            "posted_at": parse_posted_at(field_after_icon(seg, "fa-clock"), now),
        }
    return out


def _job_id_from_url(url):
    m = re.search(r"/(\d+)(?:[/?#]|$)", str(url))
    return m.group(1) if m else ""


def parse_list_page(html, now=None):
    """Normalize one listing page's HTML into job dicts. A malformed ItemList
    entry is skipped rather than aborting the whole page. The ItemList is the
    spine; card data enriches it where the ids join - a page with no
    parseable cards still yields title/url/description with empty company/location."""
    import json as _json
    jobs = []
    if not isinstance(html, str):
        return jobs
    cards = parse_cards(html, now)
    for m in ITEM.finditer(html):
        try:
            title = _json.loads(m.group(1))
            url = _json.loads(m.group(2))
            description = _json.loads(m.group(3)) if m.group(3) else ""
        except (ValueError, TypeError):
            continue
        if not title or not url:
            continue
        job = {"title": title, "url": url, "company": "", "location": "", "description": description}
        enrich = cards.get(_job_id_from_url(url))
        if enrich:
            job["company"] = enrich["company"] or ""
            job["location"] = enrich["location"] or ""
            if enrich["salary"]:
                job["salary"] = enrich["salary"]
            if enrich["posted_at"] is not None:
                job["posted_at"] = enrich["posted_at"]
        jobs.append(job)
    return jobs


def _format_salary(salary):
    """(min, max) annual USD -> the shared concise "150k-190k" format, so the
    Salary column looks the same regardless of which source it came from."""
    from job_fetch import format_salary_k
    lo, hi = salary
    return format_salary_k(lo, hi)


def fetch_builtin_jobs(queries=None, categories=None, host=None, scope=None,
                        max_pages=DEFAULT_MAX_PAGES, timeout=15, label="Built In"):
    """Scan Built In. Requires queries and/or categories - there is no default
    query set, since a shared default would bake one user's search terms into
    the pipeline (matching the reference parser's stance).

    Returns Mission Control's standard job dict shape: company, title, url,
    location, description, posted.
    """
    resolved_host = resolve_host(host)
    if resolved_host is None:
        print(f"  \u26a0 builtin: {label} has host {host!r}, which is not a known "
              f"Built In market - skipping (allowed: {', '.join(sorted(set(HOSTS.values())))})")
        return []

    queries = [str(q) for q in (queries or [])]
    categories = [str(c) for c in (categories or [])]
    scope_slug = str(scope or "").strip().lower()
    if scope_slug and not re.match(r"^[a-z0-9-]+$", scope_slug):
        print(f"  \u26a0 builtin: ignoring invalid scope {scope!r} - must be a plain slug like 'remote'")
        scope_slug = ""
    max_pages = min(HARD_MAX_PAGES, max(1, int(max_pages or DEFAULT_MAX_PAGES)))

    prefix = f"/jobs/{scope_slug}" if scope_slug else "/jobs"
    bases = [f"{prefix}?search={quote(q)}" for q in queries] + \
            [f"{prefix}/{quote(c)}" for c in categories]
    if not bases:
        print(f"  \u26a0 builtin: {label} has no queries or categories configured - nothing to scan")
        return []

    seen, out = set(), []
    guard_rows = guard_cards = guard_located = 0

    for base in bases:
        sep = "&" if "?" in base else "?"
        for page in range(1, max_pages + 1):
            url = f"https://{resolved_host}{base}{sep}page={page}"
            assert_host(url)
            try:
                resp = requests.get(url, timeout=timeout,
                                     headers={"User-Agent": BROWSER_LIKE_USER_AGENT},
                                     allow_redirects=False)
                if resp.status_code != 200:
                    break
                html = resp.text
            except requests.RequestException:
                break

            jobs = parse_list_page(html)
            guard_rows += len(jobs)
            for j in jobs:
                if j.get("company") or j.get("location"):
                    guard_cards += 1
                if j.get("location"):
                    guard_located += 1

            added = 0
            for j in jobs:
                if j["url"] in seen:
                    continue
                seen.add(j["url"])
                added += 1
                salary = j.get("salary")
                out.append({
                    "company": j.get("company") or "",
                    "title": j.get("title") or "",
                    "url": j.get("url") or "",
                    "location": j.get("location") or "",
                    "description": f"{j.get('title', '')} {j.get('description', '')}".strip(),
                    "posted": j["posted_at"].isoformat() if j.get("posted_at") else "",
                    "comp_range": _format_salary(salary) if salary else "",
                })
            if not jobs or not added:
                break  # format changed / past the last page, or a fully-overlapping tail

    if guard_rows >= GUARD_MIN_ROWS:
        if guard_cards == 0:
            print(f"  \u26a0 builtin: {label} parsed {guard_rows} rows but ZERO job cards - "
                  f"card markup changed; company/location are empty for every row.")
        elif guard_cards >= GUARD_MIN_ROWS and guard_located / guard_cards < GUARD_MIN_LOCATION:
            print(f"  \u26a0 builtin: {label} resolved a location for only "
                  f"{guard_located}/{guard_cards} cards - location markup may have changed.")

    return out

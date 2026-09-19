"""
JobSpy board aggregator (Indeed + LinkedIn by default).

Indeed has no usable public API - the Publisher API is closed and the Job Sync
API is employer/ATS-only - so the only honest options were a paid third-party
API, a maintained scraping library, or our own scraper against Cloudflare. This
uses the library: https://github.com/speedyapply/JobSpy.

Unlike builtin_source.py, which parses one board's HTML, JobSpy fronts several
boards at once (Indeed, LinkedIn, Glassdoor, ZipRecruiter, ...). It is still a
board-wide aggregator in the job-sources.yaml sense: no single company, so it is
configured under `aggregators:` with queries rather than a per-company api_url.

RATE LIMITS, AND WHY THE DEFAULTS LOOK TIMID
The upstream README is explicit: Indeed has no rate limiting and is the most
reliable scraper; LinkedIn is the most restrictive and "usually rate limits
around the 10th page with one ip", with proxies described as basically a must.
This integration assumes NO PROXIES. So:

  - queries run serially, never concurrently, with a pause between them
  - results_wanted stays small, which keeps LinkedIn well short of page 10
  - a 429 (or any repeated failure) stops that ONE site for the rest of the run
    and keeps whatever it already returned - a partial scan beats a failed one,
    the same stance builtin_source.py takes toward a changed layout
  - linkedin_fetch_description stays off; it costs one extra request per role,
    which is the fastest way to get an IP blocked

PARAMETER CONFLICTS
Indeed and LinkedIn each accept only ONE of `hours_old`, `job_type`/`is_remote`,
or `easy_apply`. Sending more silently returns nothing useful, so a conflicting
config is resolved loudly here (hours_old wins) rather than debugged later.
"""
import time
from datetime import datetime

from profile_keywords import build_board_query, matches_exclude

# Shipped default. Indeed is unlimited; LinkedIn is safe at this volume and is
# what replaced the old click-through-URL section of job-sources.yaml.
DEFAULT_SITES = ["indeed", "linkedin"]

# Google is deliberately absent: it only works with hand-copied search syntax
# pasted from a browser, which cannot be derived from a keyword list.
SUPPORTED_SITES = {"indeed", "linkedin", "glassdoor", "zip_recruiter", "bayt", "bdjobs", "naukri"}

# Sites that block aggressively without proxies. Enabling one is allowed, but
# it earns a warning and a longer pause.
RISKY_SITES = {"linkedin", "glassdoor", "zip_recruiter"}

DEFAULT_RESULTS_WANTED = 25
HARD_MAX_RESULTS = 200          # ~1000 is the board-side cap; stay far below it
DEFAULT_DELAY_SECONDS = 20
RISKY_MIN_DELAY = 10            # floor for LinkedIn & friends, even if config says 0
DEFAULT_MAX_QUERIES = 8         # one query per target keyword, but cap the fan-out
DEFAULT_COUNTRY = "USA"


def _clean(value):
    """JobSpy returns a DataFrame, so a missing field arrives as NaN, NaT or
    the string 'nan' rather than None. All of them mean 'unknown'."""
    if value is None:
        return ""
    try:
        if value != value:          # NaN/NaT are the only values unequal to themselves
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in ("nan", "nat", "none") else text


def _posted(value):
    """date_posted as a plain ISO date. An unparseable or missing date is left
    empty rather than guessed - an invented posting date silently corrupts the
    freshness filters downstream."""
    text = _clean(value)
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text[:19]).date().isoformat()
    except ValueError:
        return text[:10] if len(text) >= 10 else ""


def _comp_range(row):
    """'170K-230K' / '$55/hr' style band from JobSpy's split salary fields."""
    low, high = row.get("min_amount"), row.get("max_amount")
    try:
        low = float(low) if _clean(low) else None
        high = float(high) if _clean(high) else None
    except (TypeError, ValueError):
        return ""
    if not low and not high:
        return ""
    interval = _clean(row.get("interval")).lower()

    def money(amount):
        if amount is None:
            return ""
        if interval in ("yearly", "annual", "annually") or amount >= 1000:
            return f"{amount / 1000:.0f}K" if amount >= 1000 else f"{amount:.0f}"
        return f"{amount:.0f}"

    band = f"{money(low)}-{money(high)}" if low and high and low != high else money(low or high)
    if interval in ("hourly", "daily", "weekly", "monthly"):
        band = f"{band}/{ {'hourly': 'hr', 'daily': 'day', 'weekly': 'wk', 'monthly': 'mo'}[interval] }"
    return band


def _to_job(row, label):
    """One JobSpy row -> Mission Control's standard job dict.

    `url` stays the board URL: it is always present and stable, and the tracker
    uses it as the row's identity. job_url_direct (the employer's own posting)
    is often empty, so it is offered as a secondary link instead.
    """
    title = _clean(row.get("title"))
    url = _clean(row.get("job_url"))
    if not title or not url:
        return None
    description = _clean(row.get("description"))
    return {
        "company": _clean(row.get("company")),
        "title": title,
        "url": url,
        "location": _clean(row.get("location")),
        # Scoring reads `description`; the title is prepended so a role whose
        # keywords live only in its title still scores, matching builtin_source.
        "description": f"{title} {description}".strip(),
        "posted": _posted(row.get("date_posted")),
        "comp_range": _comp_range(row),
        "source_site": _clean(row.get("site")) or label,
        "direct_url": _clean(row.get("job_url_direct")),
    }


def _resolve_sites(agg_config, label):
    requested = agg_config.get("sites") or DEFAULT_SITES
    if isinstance(requested, str):
        requested = [requested]
    sites, risky = [], []
    for raw in requested:
        site = str(raw).strip().lower()
        if site == "google":
            print(f"  \u26a0 jobspy: {label} lists 'google', which needs hand-copied search "
                  f"syntax rather than keywords - skipping")
            continue
        if site not in SUPPORTED_SITES:
            print(f"  \u26a0 jobspy: {label} lists unknown site {raw!r} - skipping "
                  f"(known: {', '.join(sorted(SUPPORTED_SITES))})")
            continue
        if site not in sites:
            sites.append(site)
            if site in RISKY_SITES:
                risky.append(site)
    if risky:
        print(f"  \u26a0 jobspy: {', '.join(risky)} block aggressively without proxies - "
              f"keeping results_wanted low and pausing between queries")
    return sites


def _resolve_queries(agg_config, target_keywords, exclude_keywords, label):
    """Board queries come from me/profile.md's Target Keywords, one query each.

    `search_query` in the aggregator config is the escape hatch: set it and it
    is sent verbatim, for anyone who wants the boards' full query syntax.
    """
    override = agg_config.get("search_query")
    if override:
        queries = [override] if isinstance(override, str) else [str(q) for q in override]
        return [q for q in (s.strip() for s in queries) if q]

    max_queries = int(agg_config.get("max_queries") or DEFAULT_MAX_QUERIES)
    queries = []
    for term in target_keywords:
        query = build_board_query(term, exclude_keywords)
        if query and query not in queries:
            queries.append(query)
    if len(queries) > max_queries:
        print(f"  \u26a0 jobspy: {label} has {len(queries)} target keywords; using the first "
              f"{max_queries} (raise max_queries, or shorten the list in me/profile.md)")
        queries = queries[:max_queries]
    return queries


def _scrape_kwargs(agg_config, site, label, warned):
    """Per-site call arguments, with the board's own parameter conflicts resolved."""
    kwargs = {
        "site_name": [site],
        "location": agg_config.get("location") or "",
        "results_wanted": min(HARD_MAX_RESULTS,
                              max(1, int(agg_config.get("results_wanted") or DEFAULT_RESULTS_WANTED))),
        "description_format": "markdown",
        "verbose": 0,
        # O(n) extra requests, and the quickest route to a LinkedIn block.
        "linkedin_fetch_description": False,
    }
    if site in ("indeed", "glassdoor"):
        kwargs["country_indeed"] = agg_config.get("country_indeed") or DEFAULT_COUNTRY
    if agg_config.get("distance"):
        kwargs["distance"] = int(agg_config["distance"])

    hours_old = agg_config.get("hours_old")
    is_remote = agg_config.get("is_remote")
    job_type = agg_config.get("job_type")

    # Indeed and LinkedIn accept exactly one of these filter families.
    if site in ("indeed", "linkedin") and hours_old and (is_remote or job_type):
        if site not in warned:
            print(f"  \u26a0 jobspy: {site} accepts only one of hours_old / (job_type & is_remote) - "
                  f"using hours_old={hours_old} and ignoring the others for this site")
            warned.add(site)
        is_remote = job_type = None

    if hours_old:
        kwargs["hours_old"] = int(hours_old)
    if is_remote:
        kwargs["is_remote"] = bool(is_remote)
    if job_type:
        kwargs["job_type"] = str(job_type)
    return kwargs


def _is_rate_limited(error):
    text = str(error).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text or "blocked" in text


def fetch_jobspy_jobs(target_keywords, exclude_keywords=(), agg_config=None, label="JobSpy"):
    """Scan the configured boards and return Mission Control's standard job dicts.

    Mirrors builtin_source.fetch_builtin_jobs: no scoring, no tracker writes -
    just postings. Returns [] rather than raising, so one blocked board can
    never take the daily run down with it.
    """
    agg_config = agg_config or {}
    try:
        from jobspy import scrape_jobs
    except ImportError:
        print("  \u26a0 jobspy: python-jobspy is not installed - run `uv sync` (skipping)")
        return []

    sites = _resolve_sites(agg_config, label)
    if not sites:
        print(f"  \u26a0 jobspy: {label} has no usable sites configured - nothing to scan")
        return []

    queries = _resolve_queries(agg_config, target_keywords, exclude_keywords, label)
    if not queries:
        print(f"  \u26a0 jobspy: {label} has no queries - add terms under "
              f"'## Target Keywords' in me/profile.md")
        return []

    delay = max(0, int(agg_config.get("delay_seconds", DEFAULT_DELAY_SECONDS)))
    seen, out, warned = set(), [], set()
    dropped = 0

    for site in sites:
        site_delay = max(delay, RISKY_MIN_DELAY) if site in RISKY_SITES else delay
        kept_before = len(out)
        for index, query in enumerate(queries):
            try:
                frame = scrape_jobs(search_term=query,
                                    **_scrape_kwargs(agg_config, site, label, warned))
            except Exception as exc:                      # noqa: BLE001 - never fail the run
                if _is_rate_limited(exc):
                    print(f"  \u26a0 jobspy: {site} rate-limited (429) - stopping this site for "
                          f"today and keeping {len(out) - kept_before} roles already found")
                    break
                print(f"  \u26a0 jobspy: {site} query {query!r} failed ({exc}) - continuing")
                continue

            rows = frame.to_dict("records") if frame is not None and len(frame) else []
            for row in rows:
                job = _to_job(row, label)
                if not job or job["url"] in seen:
                    continue
                seen.add(job["url"])
                # The boards get the same terms as `-term`, but they honour
                # them loosely (and LinkedIn barely at all), so the exclude
                # list is enforced again here on what actually came back.
                # TITLE ONLY - the location is not part of the judgement, or a
                # city that happens to contain an excluded word drops good roles.
                hit = matches_exclude(job["title"], exclude_keywords)
                if hit:
                    dropped += 1
                    continue
                out.append(job)

            # Pause between queries, not just between sites: consecutive
            # searches from one IP are what trips the limiters.
            if site_delay and index < len(queries) - 1:
                time.sleep(site_delay)

        print(f"    {site}: {len(out) - kept_before} roles from {len(queries)} queries")

    if dropped:
        print(f"    dropped {dropped} roles on your Exclude Keywords")
    return out

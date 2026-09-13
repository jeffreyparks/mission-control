"""Deterministic job-posting extractor.

No LLM, no guessing. Given a public job-posting URL, return a flat dict with a
fixed key set. Fields that cannot be determined are None.

CLI:
    uv run python agents/job_fetch.py <url>
"""

from __future__ import annotations

import html as _html
import json
import re
import sys
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

__all__ = ["fetch_posting", "canonical_url"]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

KEYS = (
    "url",
    "org",
    "title",
    "location",
    "comp_range",
    "jd",
    "source",
    "error",
)

_TRACKING_PREFIXES = ("utm_", "mc_", "hsa_", "_hs")
_TRACKING_EXACT = {
    "gh_src",
    "gclid",
    "fbclid",
    "msclkid",
    "ref",
    "referrer",
    "source",
    "src",
    "trk",
    "trackingid",
    "li_fat_id",
    "igshid",
    "mkt_tok",
}


def _blank(url: str, source: str = "html", error: str | None = None) -> dict:
    return {
        "url": url,
        "org": None,
        "title": None,
        "location": None,
        "comp_range": None,
        "jd": None,
        "source": source,
        "error": error,
    }


def canonical_url(url: str) -> str:
    """Strip tracking query params and fragments. Keep everything else."""
    url = (url or "").strip()
    if not url:
        return url
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    kept = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low in _TRACKING_EXACT or low.startswith(_TRACKING_PREFIXES):
            continue
        kept.append((key, value))
    return urlunparse(
        (parts.scheme, parts.netloc, parts.path, parts.params, urlencode(kept), "")
    )


def _norm_space(text: str | None) -> str | None:
    """Collapse runs of whitespace but keep paragraph breaks."""
    if not text:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0\u200b]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text or None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return None
    value = _norm_space(value)
    return value


def _html_to_text(raw: str | None) -> str | None:
    if not raw:
        return None
    if "&lt;" in raw or "&gt;" in raw or "&amp;" in raw:
        raw = _html.unescape(raw)
    if "<" in raw and ">" in raw:
        soup = BeautifulSoup(raw, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        for tag in soup.find_all(["br"]):
            tag.replace_with("\n")
        for tag in soup.find_all(
            ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"]
        ):
            tag.append("\n\n")
        text = soup.get_text()
    else:
        text = _html.unescape(raw)
    return _norm_space(text)


def _get(url: str, timeout: int, accept_json: bool = False):
    headers = dict(HEADERS)
    if accept_json:
        headers["Accept"] = "application/json, text/plain, */*"
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)


# --------------------------------------------------------------------------- #
# comp range
# --------------------------------------------------------------------------- #

_MONEY = r"(?:\$|USD\s*)\s?\d[\d,\.]{2,}(?:\s*[kK])?"
_RANGE_RE = re.compile(
    _MONEY + r"\s*(?:-|–|—|to)\s*" + _MONEY, re.IGNORECASE
)


def _plausible_comp(text: str | None) -> str | None:
    """Reject junk like "0", "N/A", or bare zeros that ATS metadata often holds."""
    text = _clean_str(text)
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if not digits or int(digits) == 0:
        return None
    # a real comp figure is at least 4 significant digits (e.g. 1000) or uses k/K
    if len(digits.lstrip("0")) < 4 and not re.search(r"\d\s*[kK]\b", text):
        return None
    return text


def _comp_from_text(text: str | None) -> str | None:
    if not text:
        return None
    match = _RANGE_RE.search(text)
    if match:
        return _norm_space(match.group(0))
    return None


_ANNUAL_UNITS = {"", "year", "years", "yearly", "annual", "annually", "yr"}


def format_salary_k(low: float, high: float | None = None) -> str:
    """A dollar figure (or range) as a concise "150k-190k" string - one
    decimal only when the thousand isn't round, no "$", no comma. This is the
    canonical Salary display format; every structured source (Ashby, Built In)
    formats through this so the column looks the same regardless of origin.
    """
    def one(value: float) -> str:
        k = value / 1000
        text = f"{k:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"

    if high is None or high == low:
        return one(low)
    return f"{one(low)}-{one(high)}"


def _comp_from_base_salary(base: Any) -> str | None:
    """schema.org MonetaryAmount -> concise text.

    Non-USD currencies and non-annual units (hourly, per-project, ...) are kept
    in dollar-and-unit form rather than forced through the k-notation, which
    only makes sense for an annual figure - "45k/hour" would misread as an
    annual salary two orders of magnitude off.
    """
    if not isinstance(base, dict):
        return _clean_str(base)
    value = base.get("value")
    currency = base.get("currency") or base.get("currencyCode") or ""
    is_usd = currency.upper() in ("USD", "")
    symbol = "$" if is_usd else currency.upper() + " "
    if isinstance(value, dict):
        low = value.get("minValue")
        high = value.get("maxValue")
        single = value.get("value")
        unit = (value.get("unitText") or "").strip().lower()
        concise = is_usd and unit in _ANNUAL_UNITS
        if low is not None and high is not None:
            if concise:
                try:
                    return format_salary_k(float(low), float(high))
                except (TypeError, ValueError):
                    pass
            out = f"{symbol}{_fmt_num(low)} - {symbol}{_fmt_num(high)}"
        elif single is not None:
            if concise:
                try:
                    return format_salary_k(float(single))
                except (TypeError, ValueError):
                    pass
            out = f"{symbol}{_fmt_num(single)}"
        else:
            return None
        if unit and not concise:
            out += f" per {unit}"
        return out
    if value is not None:
        if is_usd:
            try:
                return format_salary_k(float(value))
            except (TypeError, ValueError):
                pass
        return f"{symbol}{_fmt_num(value)}"
    return None


def _fmt_num(value: Any) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num == int(num):
        return f"{int(num):,}"
    return f"{num:,.2f}"


# --------------------------------------------------------------------------- #
# Greenhouse
# --------------------------------------------------------------------------- #

_GH_RE = re.compile(
    r"^(?:job-boards|boards|boards-api)(?:\.eu)?\.greenhouse\.io$", re.IGNORECASE
)
_GH_PATH_RE = re.compile(r"^/(?:embed/job_app\?|)?([^/]+)/jobs/(\d+)")


def _greenhouse_ids(url: str) -> tuple[str, str] | None:
    parts = urlparse(url)
    if not _GH_RE.match(parts.netloc or ""):
        return None
    match = _GH_PATH_RE.match(parts.path or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def _from_greenhouse(url: str, org_slug: str, job_id: str, timeout: int) -> dict:
    out = _blank(url, source="greenhouse")
    api = (
        f"https://boards-api.greenhouse.io/v1/boards/{org_slug}"
        f"/jobs/{job_id}?content=true"
    )
    try:
        resp = _get(api, timeout, accept_json=True)
    except requests.RequestException as exc:
        out["error"] = f"greenhouse api request failed: {exc}"
        return out
    if resp.status_code != 200:
        out["error"] = f"greenhouse api returned HTTP {resp.status_code}"
        return out
    try:
        data = resp.json()
    except ValueError as exc:
        out["error"] = f"greenhouse api returned non-json: {exc}"
        return out
    if not isinstance(data, dict):
        out["error"] = "greenhouse api returned unexpected json shape"
        return out

    out["title"] = _clean_str(data.get("title"))
    loc = data.get("location")
    if isinstance(loc, dict):
        out["location"] = _clean_str(loc.get("name"))
    else:
        out["location"] = _clean_str(loc)

    company = data.get("company_name")
    out["org"] = _clean_str(company) or None

    out["jd"] = _html_to_text(data.get("content"))

    # comp: greenhouse sometimes exposes pay_input_ranges / metadata
    comp = None
    ranges = data.get("pay_input_ranges")
    if isinstance(ranges, list) and ranges:
        first = ranges[0]
        if isinstance(first, dict):
            low = first.get("min_cents")
            high = first.get("max_cents")
            currency = first.get("currency_type") or "USD"
            symbol = "$" if currency.upper() == "USD" else currency.upper() + " "
            if low and high:
                comp = f"{symbol}{_fmt_num(low / 100)} - {symbol}{_fmt_num(high / 100)}"
    if comp is None:
        for entry in data.get("metadata") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").lower()
            if "salary" in name or "pay" in name or "comp" in name:
                comp = _clean_str(entry.get("value"))
                if comp:
                    break
    comp = _plausible_comp(comp)
    if comp is None:
        comp = _comp_from_text(out["jd"])
    out["comp_range"] = comp
    return out


# --------------------------------------------------------------------------- #
# Lever
# --------------------------------------------------------------------------- #

_LEVER_RE = re.compile(r"^(?:jobs|api)\.(?:eu\.)?lever\.co$", re.IGNORECASE)


def _lever_ids(url: str) -> tuple[str, str] | None:
    parts = urlparse(url)
    if not _LEVER_RE.match(parts.netloc or ""):
        return None
    segs = [s for s in (parts.path or "").split("/") if s]
    if len(segs) < 2:
        return None
    org, posting = segs[0], segs[1]
    if posting in ("apply", "thanks"):
        return None
    return org, posting


def _from_lever(url: str, org_slug: str, posting_id: str, timeout: int) -> dict:
    out = _blank(url, source="lever")
    api = f"https://api.lever.co/v0/postings/{org_slug}/{posting_id}"
    try:
        resp = _get(api, timeout, accept_json=True)
    except requests.RequestException as exc:
        out["error"] = f"lever api request failed: {exc}"
        return out
    if resp.status_code != 200:
        out["error"] = f"lever api returned HTTP {resp.status_code}"
        return out
    try:
        data = resp.json()
    except ValueError as exc:
        out["error"] = f"lever api returned non-json: {exc}"
        return out
    if not isinstance(data, dict):
        out["error"] = "lever api returned unexpected json shape"
        return out

    out["title"] = _clean_str(data.get("text"))
    cats = data.get("categories") if isinstance(data.get("categories"), dict) else {}
    out["location"] = _clean_str(cats.get("location") or data.get("country"))

    pieces = []
    desc = _html_to_text(data.get("description") or data.get("descriptionPlain"))
    if desc:
        pieces.append(desc)
    for section in data.get("lists") or []:
        if not isinstance(section, dict):
            continue
        head = _clean_str(section.get("text"))
        body = _html_to_text(section.get("content"))
        if head:
            pieces.append(head)
        if body:
            pieces.append(body)
    tail = _html_to_text(data.get("additional") or data.get("additionalPlain"))
    if tail:
        pieces.append(tail)
    out["jd"] = _norm_space("\n\n".join(pieces)) if pieces else None

    out["comp_range"] = _clean_str(data.get("salaryRange")) or _comp_from_text(out["jd"])
    if isinstance(data.get("salaryRange"), dict):
        rng = data["salaryRange"]
        low, high = rng.get("min"), rng.get("max")
        currency = rng.get("currency") or "USD"
        symbol = "$" if str(currency).upper() == "USD" else str(currency).upper() + " "
        if low is not None and high is not None:
            out["comp_range"] = f"{symbol}{_fmt_num(low)} - {symbol}{_fmt_num(high)}"

    # Lever's API does not return a display company name; derive nothing.
    out["org"] = None
    return out


# --------------------------------------------------------------------------- #
# Ashby
# --------------------------------------------------------------------------- #

_ASHBY_RE = re.compile(r"^jobs\.ashbyhq\.com$", re.IGNORECASE)


def _ashby_ids(url: str) -> tuple[str, str] | None:
    parts = urlparse(url)
    if not _ASHBY_RE.match(parts.netloc or ""):
        return None
    segs = [s for s in (parts.path or "").split("/") if s]
    if len(segs) < 2:
        return None
    return segs[0], segs[1]


_ASHBY_QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
    jobPostingId: $jobPostingId
  ) {
    title
    departmentName
    locationName
    descriptionHtml
    compensationTierSummary
  }
}
"""


def _from_ashby(url: str, org_slug: str, posting_id: str, timeout: int) -> dict | None:
    """Return a filled dict, or None to fall through to JSON-LD/HTML."""
    api = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
    payload = {
        "operationName": "ApiJobPosting",
        "variables": {
            "organizationHostedJobsPageName": org_slug,
            "jobPostingId": posting_id,
        },
        "query": _ASHBY_QUERY,
    }
    try:
        resp = requests.post(
            api,
            json=payload,
            headers={**HEADERS, "Accept": "application/json", "Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    posting = (data or {}).get("data", {}).get("jobPosting")
    if not isinstance(posting, dict) or not posting.get("title"):
        return None
    out = _blank(url, source="ashby")
    out["title"] = _clean_str(posting.get("title"))
    out["location"] = _clean_str(posting.get("locationName"))
    out["jd"] = _html_to_text(posting.get("descriptionHtml"))
    out["comp_range"] = _clean_str(posting.get("compensationTierSummary")) or _comp_from_text(
        out["jd"]
    )
    # The Ashby posting API exposes no company display name; leave org unset and
    # let the JSON-LD on the page supply it when present.
    out["org"] = None
    return out


# --------------------------------------------------------------------------- #
# JSON-LD
# --------------------------------------------------------------------------- #


def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            # some sites emit trailing commas / concatenated objects
            try:
                parsed = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except ValueError:
                continue
        stack = [parsed]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.append(node["@graph"])
                yield node


def _is_job_posting(node: dict) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return any(str(t).lower() == "jobposting" for t in node_type)
    return str(node_type or "").lower() == "jobposting"


def _location_from_jsonld(node: dict) -> str | None:
    raw = node.get("jobLocation")
    if node.get("jobLocationType") and not raw:
        return _clean_str(node.get("jobLocationType"))
    candidates = raw if isinstance(raw, list) else [raw]
    names = []
    for cand in candidates:
        if isinstance(cand, str):
            names.append(cand)
            continue
        if not isinstance(cand, dict):
            continue
        addr = cand.get("address")
        if isinstance(addr, str):
            names.append(addr)
            continue
        if isinstance(addr, dict):
            bits = [
                addr.get("addressLocality"),
                addr.get("addressRegion"),
                addr.get("addressCountry")
                if not isinstance(addr.get("addressCountry"), dict)
                else addr["addressCountry"].get("name"),
            ]
            joined = ", ".join([str(b) for b in bits if b])
            if joined:
                names.append(joined)
                continue
        if cand.get("name"):
            names.append(str(cand["name"]))
    names = [n for n in (_clean_str(n) for n in names) if n]
    # de-dup, preserve order
    seen, uniq = set(), []
    for name in names:
        if name not in seen:
            seen.add(name)
            uniq.append(name)
    return "; ".join(uniq) if uniq else None


def _from_jsonld(url: str, soup: BeautifulSoup) -> dict | None:
    for node in _iter_jsonld(soup):
        if not _is_job_posting(node):
            continue
        out = _blank(url, source="jsonld")
        out["title"] = _clean_str(node.get("title")) or _clean_str(node.get("name"))
        org = node.get("hiringOrganization")
        if isinstance(org, dict):
            out["org"] = _clean_str(org.get("name") or org.get("legalName"))
        else:
            out["org"] = _clean_str(org)
        out["location"] = _location_from_jsonld(node)
        out["jd"] = _html_to_text(node.get("description"))
        out["comp_range"] = _comp_from_base_salary(
            node.get("baseSalary")
        ) or _comp_from_text(out["jd"])
        if out["title"] or out["jd"]:
            return out
    return None


# --------------------------------------------------------------------------- #
# Generic HTML
# --------------------------------------------------------------------------- #

_TITLE_SPLIT = re.compile(r"\s+[|\u2013\u2014\-\u00b7]\s+")


def _meta(soup: BeautifulSoup, *, prop: str | None = None, name: str | None = None):
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return _clean_str(tag["content"])
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_str(tag["content"])
    return None


# Job-board suffixes that appear after a "|" or "-" in page titles.
_SITE_SUFFIXES = {
    "linkedin", "indeed", "glassdoor", "built in", "builtin", "greenhouse",
    "lever", "ashby", "ashbyhq", "workday", "smartrecruiters", "jobvite",
    "wellfound", "angellist", "ziprecruiter", "dice", "monster", "careers",
    "job board", "jobs", "hiring", "welcome to the jungle", "otta",
    "career page", "careers page", "career site", "job details", "apply",
}

# "Acme hiring Senior Director, Data Science in New York, NY"
_HIRING_RE = re.compile(
    r"^(?P<org>.{2,80}?)\s+hiring\s+(?P<title>.+?)"
    r"(?:\s+in\s+(?P<loc>[^|]+?))?\s*$",
    re.I,
)


def _strip_site_suffix(title: str) -> str:
    """Drop trailing board names: 'Role | LinkedIn' -> 'Role'."""
    for _ in range(3):
        parts = re.split(r"\s*[|\u00b7]\s*|\s+[\u2013\u2014]\s+", title)
        if len(parts) > 1 and parts[-1].strip().lower() in _SITE_SUFFIXES:
            title = " ".join(parts[:-1]).strip()
            continue
        match = re.match(r"^(?P<head>.+?)\s+-\s+(?P<tail>[\w ]{2,30})$", title)
        if match and match.group("tail").strip().lower() in _SITE_SUFFIXES:
            title = match.group("head").strip()
            continue
        break
    return title.strip(" -|\u00b7")


def normalise_title(title, org=None):
    """Clean a scraped page title down to the role name.

    Returns (title, org). Org is only returned when the page title revealed it
    and we did not already have one.
    """
    if not title:
        return title, org

    title = _strip_site_suffix(_clean_str(title) or "")

    # "{Org} hiring {Title} in {Location}"
    match = _HIRING_RE.match(title)
    if match:
        found_org = _clean_str(match.group("org"))
        title = _clean_str(match.group("title")) or title
        if found_org and not org:
            org = found_org

    # Drop a leading org prefix: "Acme - Acme Senior Director" / "Acme: Senior Director"
    if org:
        title = re.sub(
            rf"^(?:{re.escape(org)}\s*[-:\u2013\u2014|]\s*)+", "", title, flags=re.I
        ).strip()
        # a repeated org word left at the front, e.g. "Paramount Paramount Senior..."
        title = re.sub(rf"^(?:{re.escape(org)}\s+)(?={re.escape(org)}\s)", "", title, flags=re.I)
        # Trailing employer segment: "Head of X - Acme Organics" -> "Head of X"
        title = re.sub(
            rf"\s+[-\u2013\u2014|]\s+{re.escape(org)}\b[\w &.,'-]*$", "", title, flags=re.I
        ).strip()

    # Trailing " in New York, NY" that survived a non-matching hiring pattern
    title = re.sub(r"\s+in\s+[A-Z][\w .'-]*,\s*[A-Z]{2}$", "", title).strip()

    return (_clean_str(title) or None), org


def _from_html(url: str, soup: BeautifulSoup) -> dict:
    out = _blank(url, source="html")

    title = _meta(soup, prop="og:title") or _meta(soup, name="twitter:title")
    if not title and soup.title:
        title = _clean_str(soup.title.get_text())
    site = _meta(soup, prop="og:site_name")

    if title and site and site.lower() in title.lower():
        parts = [p for p in _TITLE_SPLIT.split(title) if site.lower() not in p.lower()]
        if parts:
            title = _clean_str(" - ".join(parts))
    org = site
    if org and org.strip().lower() in _SITE_SUFFIXES:
        org = None  # "LinkedIn" is not the employer
    title, org = normalise_title(title, org)
    out["title"] = title
    out["org"] = org or _clean_str(urlparse(url).netloc or None)

    body = soup.find("main") or soup.find(
        "div", attrs={"id": re.compile("content|main", re.I)}
    ) or soup.body
    if body is not None:
        for tag in body(["script", "style", "nav", "footer", "header", "form", "noscript"]):
            tag.decompose()
        text = _html_to_text(str(body))
        # JS-app shells sometimes leave a raw JSON config blob as the only text
        if text and text.lstrip()[:1] in "{[" and text.count('":') > 5:
            text = None
        out["jd"] = text
    if not out["jd"]:
        out["jd"] = _meta(soup, prop="og:description") or _meta(soup, name="description")
    out["comp_range"] = _comp_from_text(out["jd"])
    if out["jd"] and len(out["jd"]) < 200:
        out["error"] = "page body text too short to be a job description"
    return out


# --------------------------------------------------------------------------- #
# public entrypoint
# --------------------------------------------------------------------------- #


def fetch_posting(url: str, timeout: int = 20) -> dict:
    """Extract a job posting deterministically. Never raises for network errors."""
    clean = canonical_url(url)
    if not clean or not urlparse(clean).scheme.startswith("http"):
        out = _blank(clean or (url or ""), error="not an http(s) url")
        return {k: out[k] for k in KEYS}

    result: dict | None = None
    try:
        ids = _greenhouse_ids(clean)
        if ids:
            result = _from_greenhouse(clean, ids[0], ids[1], timeout)
            # A greenhouse job URL that the API rejects is a dead/invalid job.
            # Falling through to the HTML board page would yield a wrong title.
            return {k: result[k] for k in KEYS}

        ids = _lever_ids(clean)
        if ids:
            lever = _from_lever(clean, ids[0], ids[1], timeout)
            return {k: lever[k] for k in KEYS}

        ids = _ashby_ids(clean)
        if ids:
            ashby = _from_ashby(clean, ids[0], ids[1], timeout)
            if ashby:
                # Keep it as a fallback; the page's JSON-LD usually also
                # carries the hiring organization name, which the API omits.
                result = ashby

        # page fetch for JSON-LD / generic HTML
        try:
            resp = _get(clean, timeout)
        except requests.RequestException as exc:
            if result and not result.get("error"):
                return {k: result[k] for k in KEYS}
            out = result or _blank(clean)
            out["error"] = out.get("error") or f"request failed: {exc}"
            return {k: out[k] for k in KEYS}

        if resp.status_code != 200:
            if result and not result.get("error"):
                return {k: result[k] for k in KEYS}
            out = result or _blank(clean)
            out["error"] = out.get("error") or f"HTTP {resp.status_code}"
            return {k: out[k] for k in KEYS}

        final_url = canonical_url(resp.url or clean)
        soup = BeautifulSoup(resp.text, "lxml")

        jsonld = _from_jsonld(final_url, soup)
        if jsonld:
            return {k: jsonld[k] for k in KEYS}

        if result and not result.get("error"):
            return {k: result[k] for k in KEYS}

        generic = _from_html(final_url, soup)
        if not generic["jd"]:
            # No readable body text: a page <title> alone is not a job posting.
            # Do not guess an org/title from a shell page.
            generic = _blank(
                final_url, error="no job-posting body text found in page"
            )
        return {k: generic[k] for k in KEYS}
    except Exception as exc:  # deterministic: never leak an exception
        out = result or _blank(clean)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return {k: out[k] for k in KEYS}


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python agents/job_fetch.py <url>", file=sys.stderr)
        return 2
    print(json.dumps(fetch_posting(argv[1]), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

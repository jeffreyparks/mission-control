"""
Job Intelligence Agent (daily).

Replaces the substring keyword scoring in job_scanner.py with real LLM judgment.
Fetching stays where it works: JobScanner.fetch_greenhouse_jobs / fetch_lever_jobs.

WHAT IT DOES
  1. FIT ANALYSIS   - every tracker role + a prefiltered slice of live roles is judged
                      by the LLM in batches (~16 roles per call), against the candidate
                      context block (me/profile.md + resume).
  2. ORG ENRICHMENT - one batched call labels each distinct org with a sector sub-line
                      ("Tech - Advertising", "Healthcare - Health Tech"). Cached on disk.
  3. OUTCOME TIMING - parses free-text Outcomes ("Rejected (2d)") and/or Date Applied vs
                      Last Updated into a numeric days_to_outcome.
  4. TRACKER UPDATE - timestamped backup, then adds 5 columns. Existing rows/values and
                      the user's manual columns are never overwritten.

OUTPUT: artifacts/jobs/intel-YYYY-MM-DD.json

{
  "generated_at":  ISO8601 str,
  "date":          "YYYY-MM-DD",
  "counts":        {"roles": int, "tracker_roles": int, "live_roles": int,
                    "orgs": int, "apply": int, "research": int, "skip": int},
  "llm":           str,                       # cost / cache report line
  "roles": [
    {
      "id":            str,                   # stable slug: org--title
      "source":        "tracker" | "live",    # tracker row, or freshly fetched posting
      "org":           str,
      "title":         str,
      "sector":        str | null,            # e.g. "Tech - Advertising"
      "url":           str | null,
      "location":      str | null,
      "role_cat":      str | null,            # user column, tracker rows only
      "priority":      int | null,            # user column, tracker rows only
      "status":        str | null,            # user column, tracker rows only
      "date_opened":   "YYYY-MM-DD" | null,
      "date_applied":  "YYYY-MM-DD" | null,
      "outcome":       str | null,            # raw Outcomes cell
      "outcome_label": str | null,            # "Rejected" / "Interview" / ...
      "days_to_outcome": int | null,          # elapsed days, for "Rejected (2d)" UI
      "outcome_display": str | null,          # prerendered "Rejected (2d)"
      "notes":         str | null,

      "fit_score":     int | null,            # 0-100, calibrated; most roles are low
      "seniority_read": str | null,           # IC vs manager / level read
      "why":           str | null,            # 2-3 sentences
      "top_gaps":      [str],                 # 2-3 concrete gaps
      "what_theyre_really_hiring_for": str | null,
      "pitch_angle":   str | null,            # null when it is a bad fit
      "recommendation": "apply" | "research" | "skip" | null,

      "suggested_role_cat":     str | null,   # archetype proposal, only when role_cat is empty
      "suggestion_confidence":  "high" | "medium" | "low" | null,
      "suggestion_reason":      str | null    # one short clause, or null for "none"
    }
  ],
  "orgs": [                                   # directory, aggregated per company
    {
      "org": str,
      "sector": str | null,
      "role_count": int,
      "open_count": int,
      "applied_count": int,
      "closed_count": int,
      "status_mix": {status: count},
      "recommendation_mix": {"apply": int, "research": int, "skip": int},
      "best_fit_score": int | null,
      "median_fit_score": int | null,
      "best_role": {"title": str, "fit_score": int, "url": str | null} | null,
      "outcomes": [{"title": str, "outcome": str, "days_to_outcome": int | null}]
    }
  ]
}

Tracker columns added (never overwriting Role Cat / Priority / Status / Outcomes / Notes):
    Fit Score, Fit Rationale, Recommendation, Sector, Days To Outcome,
    Role Cat (suggested)   <- machine proposal, written only when Role Cat is empty

Run:  uv run python agents/job_intel.py
"""
import argparse
import hashlib
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from llm import LLM, output_budget, LLMError            # noqa: E402
from context import context_block        # noqa: E402
from job_scanner import JobScanner       # noqa: E402
from store import COLUMN_MAP             # noqa: E402 - the column contract only, not the Store


# Titles worth spending judgment on. Deliberately generous: the LLM does the
# real filtering, this only keeps the batch bill sane.
TITLE_PATTERN = re.compile(
    r"data scien|analytic|measurement|marketing scien|experiment|attribution|"
    r"incremental|econometric|economist|insights|research scien|applied scien|"
    r"machine learning|ml engineer|forward deployed|applied ai|solutions architect|"
    r"growth|strategist|marketing",
    re.I,
)

# Ranked prefilter: higher tier survives the cap first.
TIER_1 = re.compile(r"measurement|marketing scien|attribution|incremental|experiment|econometric|marketing", re.I)
TIER_2 = re.compile(r"data scien|applied ai|forward deployed|insights|analytic|economist", re.I)

MANUAL_COLUMNS = ["Role Cat", "Priority", "Status", "Outcomes", "Notes"]

# Machine suggestion column. Deliberately NOT "Role Cat": that one is the user's.
SUGGESTED_CAT_COLUMN = "Role Cat (suggested)"

# Bump when the categorisation prompt changes.
CAT_PROMPT_VERSION = 1

# Bump when the fit prompt changes; it invalidates every stored judgment.
PROMPT_VERSION = 1

# Live finds are appended to the tracker automatically under this status, so the
# record stays a complete physical record. Set AUTO_ADD_LIVE = False to keep
# the tracker hand-curated.
AUTO_ADD_LIVE = True
NEW_FIND_STATUS = "00 New find"

# The live-scan judging queue is dominated by whichever source has the most
# volume, and that is usually the priority-1 aggregators (Built In). This
# reserves a floor of the daily cap for everything else - named companies at
# priority > 1 - so they are never fully crowded out. If the lower tiers do
# not have enough candidates to fill their reserve, aggregator postings take
# the unused slots instead, and the cap stays fully used either way.
AGGREGATOR_PRIORITY = 1
LOWER_TIER_RESERVE_FRACTION = 0.25

# Only prune backups this agent made. Anything else in that folder is left alone.

# Written by merge_fit, carried forward verbatim when a role is unchanged.
FIT_FIELDS = [
    "fit_score", "seniority_read", "why", "top_gaps",
    "what_theyre_really_hiring_for", "pitch_angle", "recommendation",
]

def load_auto_close_titles(scanner):
    """Title phrases that skip the LLM fit call entirely for a brand-new live
    find, closing it immediately. Editable in config/job-sources.yaml under
    rules.auto_close_titles - reuses JobScanner's own YAML loader rather than
    parsing the file a second time."""
    config = scanner.load_sources() or {}
    titles = (config.get("rules") or {}).get("auto_close_titles") or []
    return [str(t).strip() for t in titles if str(t).strip()]


DEFAULT_MAX_LIVE_ROLES = 200

# Roles per fit call. Measured over 499 real verdicts: median 216 output tokens
# per role, p90 307, max 480. Six roles is small enough that each gets real
# attention and the answer never approaches the output ceiling, while still
# showing the model a spread of roles - the prompt's calibration depends on
# that ("a batch where many roles score high is a failed batch"), so judging
# one role in isolation would lose the comparison that keeps scores honest.
DEFAULT_FIT_BATCH_SIZE = 6

# Categorisation is a short mechanical label per role, routed to a cheap model,
# so it batches far wider than fit judgement. Sized so a full chunk's answer
# stays well inside the output budget: ids are long slugs, so ~50 tokens a row.
CAT_BATCH_SIZE = 50
CAT_TOKENS_PER_ROLE = 150


def load_fit_batch_size(scanner):
    """Roles per fit call. Editable in config/job-sources.yaml under
    rules.fit_batch_size."""
    config = scanner.load_sources() or {}
    value = (config.get("rules") or {}).get("fit_batch_size")
    try:
        return max(1, int(value)) if value is not None else DEFAULT_FIT_BATCH_SIZE
    except (TypeError, ValueError):
        return DEFAULT_FIT_BATCH_SIZE


def load_max_live_roles(scanner):
    """Cap on freshly-scraped postings judged in one run. Editable in
    config/job-sources.yaml under rules.max_live_roles - same config, same
    per-workspace override, as auto_close_titles above. None/absent falls back
    to DEFAULT_MAX_LIVE_ROLES; a CLI --max-live still wins over both."""
    config = scanner.load_sources() or {}
    value = (config.get("rules") or {}).get("max_live_roles")
    try:
        return int(value) if value is not None else DEFAULT_MAX_LIVE_ROLES
    except (TypeError, ValueError):
        return DEFAULT_MAX_LIVE_ROLES


def matched_auto_close_title(title, patterns):
    """The first pattern that matches (case-insensitive, whole-phrase), or
    None. Word-boundaried so "Machine Learning Engineer" matches "Senior
    Machine Learning Engineer" but not the "Engineer" inside "Engineering"."""
    text = title or ""
    for pattern in patterns:
        if re.search(rf"\b{re.escape(pattern)}\b", text, re.I):
            return pattern
    return None
NEW_COLUMNS = ["Fit Score", "Fit Rationale", "Recommendation", "Sector", "Days To Outcome",
               SUGGESTED_CAT_COLUMN]

# Written by merge_categories, carried forward verbatim when a role is unchanged.
CAT_FIELDS = ["suggested_role_cat", "suggestion_confidence", "suggestion_reason"]


def _slug(org, title):
    raw = f"{org}--{title}".lower()
    return re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:120]


def _clean(value):
    """pandas cell -> str|None."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if ts is None or pd.isna(ts):
        return None
    return ts


def _strip_html(text, limit=900):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#39;", "'").replace("&quot;", '"').replace("&nbsp;", " "))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# --------------------------------------------------------------------------
# outcome timing
# --------------------------------------------------------------------------

OUTCOME_LABELS = [
    ("rejected", "Rejected"), ("reject", "Rejected"), ("declined", "Rejected"),
    ("no response", "No response"), ("ghost", "Ghosted"),
    ("offer", "Offer"), ("onsite", "Onsite"), ("final", "Final round"),
    ("interview", "Interview"), ("screen", "Screen"), ("recruiter", "Recruiter call"),
    ("withdrew", "Withdrew"), ("withdrawn", "Withdrew"), ("closed", "Closed"),
]

_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*(d|day|days|w|wk|week|weeks|m|mo|month|months|h|hr|hour|hours)\b", re.I)


def parse_outcome(outcome_text, date_applied=None, last_updated=None):
    """Return (label, days_to_outcome, display). Any part may be None."""
    text = _clean(outcome_text)
    label = None
    days = None

    if text:
        low = text.lower()
        for needle, pretty in OUTCOME_LABELS:
            if needle in low:
                label = pretty
                break
        if label is None:
            label = text.split("(")[0].strip().title() or None

        match = _DUR.search(low)
        if match:
            amount = float(match.group(1))
            unit = match.group(2).lower()
            if unit.startswith("h"):
                days = max(int(round(amount / 24.0)), 0)
            elif unit.startswith("w"):
                days = int(round(amount * 7))
            elif unit.startswith("m"):
                days = int(round(amount * 30))
            else:
                days = int(round(amount))

    # Fall back to date arithmetic when the free text carried no duration.
    if days is None:
        applied = _date(date_applied)
        end = _date(last_updated)
        if applied is not None and end is not None and end >= applied:
            delta = (end - applied).days
            if 0 <= delta <= 400 and text:
                days = int(delta)

    display = None
    if label and days is not None:
        display = f"{label} ({days}d)"
    elif label:
        display = label
    return label, days, display


# --------------------------------------------------------------------------
# role archetypes (me/profile.md "## Target Roles")
# --------------------------------------------------------------------------

_TARGET_ROLE_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")


def load_archetypes(base_dir, tracker_df=None):
    """Return [{"id": "01", "label": ..., "description": ...}] from me/profile.md.

    The numbered list under "## Target Roles" is the source of truth for the set.
    Where the user already uses a label in the tracker's Role Cat column for the
    same number, that exact label wins, so suggestions read like the user's own
    vocabulary instead of a paraphrase.
    """
    path = Path(base_dir) / "me/profile.md"
    if not path.exists():
        return []

    lines, collecting = [], False
    for line in path.read_text().splitlines():
        if line.strip().startswith("## "):
            collecting = line.strip().lower().startswith("## target roles")
            continue
        if collecting:
            match = _TARGET_ROLE_RE.match(line)
            if match:
                lines.append((f"{int(match.group(1)):02d}", match.group(2).strip()))

    # Labels the user actually types, keyed by leading number.
    user_labels = {}
    if tracker_df is not None and "Role Cat" in getattr(tracker_df, "columns", []):
        for value in tracker_df["Role Cat"].dropna().unique():
            text = str(value).strip()
            head = re.match(r"^(\d+)\b", text)
            if head:
                user_labels.setdefault(f"{int(head.group(1)):02d}", text)

    return [
        {"id": cid, "label": user_labels.get(cid, f"{cid} {desc}"), "description": desc}
        for cid, desc in lines
    ]


# --------------------------------------------------------------------------
# agent
# --------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# response validators
#
# These run inside LLM.complete_json. A cheap model that returns the wrong
# shape, drops roles, or invents a score outside the band is rejected exactly
# like unparseable output: retried once, then escalated up the tier ladder to
# the frontier model. This is what makes cheap routing safe.
# ---------------------------------------------------------------------------

_SECTOR_RE = re.compile(r"^[\w &.+/'-]{1,30} - [\w &.+/'-]{1,30}$")

FIT_REQUIRED = ("id", "fit_score", "seniority_read", "why", "top_gaps",
                "what_theyre_really_hiring_for", "recommendation")


def _as_list(data):
    if isinstance(data, dict):
        for key in ("roles", "results", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return None
    return data if isinstance(data, list) else None


def make_fit_validator(batch):
    wanted = {role["id"] for role in batch}

    def validate(data):
        rows = _as_list(data)
        if not rows:
            return False
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not all(f in row for f in FIT_REQUIRED):
                return False
            score = row.get("fit_score")
            if not isinstance(score, int) or not 0 <= score <= 100:
                return False
            if row.get("recommendation") not in ("apply", "research", "skip"):
                return False
            seen.add(row.get("id"))
        # Allow a couple of dropped roles on a big batch, but not a gutted answer.
        return len(wanted & seen) >= max(1, int(len(wanted) * 0.8))

    return validate


def make_cat_validator(batch, valid_ids):
    wanted = {role["id"] for role in batch}
    allowed = set(valid_ids) | {"none"}

    def validate(data):
        rows = _as_list(data)
        if not rows:
            return False
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                return False
            if str(row.get("archetype")) not in allowed:
                return False
            if row.get("confidence") not in ("high", "medium", "low"):
                return False
            seen.add(row.get("id"))
        return len(wanted & seen) >= max(1, int(len(wanted) * 0.8))

    return validate


def make_sector_validator(orgs):
    wanted = set(orgs)

    def validate(data):
        if not isinstance(data, dict) or not data:
            return False
        hits = 0
        for org in wanted:
            value = data.get(org)
            if isinstance(value, str) and _SECTOR_RE.match(value.strip()):
                hits += 1
        return hits >= max(1, int(len(wanted) * 0.8))

    return validate


class JobIntel:
    def __init__(self, base_dir, batch_size=None, max_live_roles=None, model=None, refresh=False):
        self.refresh = refresh
        self._jd_cache = None
        self._store = None
        self.base_dir = Path(base_dir)
        self.sector_cache_path = self.base_dir / "data/org-sectors.json"
        self.scanner = JobScanner(self.base_dir)
        self.batch_size = load_fit_batch_size(self.scanner) if batch_size is None else batch_size
        # None means "use this workspace's config" (rules.max_live_roles in
        # job-sources.yaml, default 200); an explicit value - CLI --max-live,
        # or add_role.py's 0 - always wins over the config.
        self.max_live_roles = load_max_live_roles(self.scanner) if max_live_roles is None else max_live_roles
        self.llm = LLM(self.base_dir, model=model)
        self.ctx = context_block(self.base_dir)

    # ---------------- role universe ----------------

    @property
    def store(self):
        """The SQLite source of record. Created lazily so a bare JobIntel() used
        for prompt inspection never touches the database."""
        if self._store is None:
            from store import Store
            self._store = Store(self.base_dir)
        return self._store

    def load_tracker(self):
        """Roles from the database.

        A workspace with nothing yet gets an empty frame that still has the real
        columns. It used to get a bare DataFrame, which every caller then had to
        treat as "no tracker at all" - so a brand new workspace could never
        write its first rows, never got store ids, and its dashboard stayed
        permanently read-only. An empty tracker is a tracker with no rows."""
        if self.store.is_empty():
            return pd.DataFrame(columns=list(COLUMN_MAP.keys()))
        return self.store.load()

    def save_tracker(self, df, actor="pipeline"):
        """Persist to the database - the only record. The CSV copy is written
        once at the end of the run, not on every save."""
        return self.store.save_df(df, actor=actor)

    def tracker_roles(self, df):
        roles, cache = [], self.load_jd_cache()
        for idx, row in df.iterrows():
            org = _clean(row.get("Org")) or "Unknown"
            title = _clean(row.get("Title")) or "Untitled"
            applied = _date(row.get("Date Applied"))
            opened = _date(row.get("Date Opened"))
            label, days, display = parse_outcome(
                row.get("Outcomes"), row.get("Date Applied"), row.get("Last Updated")
            )
            roles.append({
                "id": _slug(org, title),
                # The store's primary key. Differs from "id" only when two rows
                # share an org and title; the dashboard writes against this.
                "store_id": _clean(row.get("_id")) or _slug(org, title),
                "row_index": int(idx),
                "source": "tracker",
                "org": org,
                "title": title,
                "url": _clean(row.get("Role Link")),
                "location": None,
                "role_cat": _clean(row.get("Role Cat")),
                "priority": int(row["Priority"]) if _clean(row.get("Priority")) else None,
                "status": _clean(row.get("Status")),
                "date_opened": opened.strftime("%Y-%m-%d") if opened is not None else None,
                "date_applied": applied.strftime("%Y-%m-%d") if applied is not None else None,
                "outcome": _clean(row.get("Outcomes")),
                "outcome_label": label,
                "days_to_outcome": days,
                "outcome_display": display,
                "notes": _clean(row.get("Notes")),
                "comp_range": _clean(row.get("Range")),
                "jd": "",
            })
            # A role added by URL carries a real description; use it.
            extra = cache.get(roles[-1]["id"])
            if extra:
                for field in ("jd", "location", "comp_range"):
                    if extra.get(field) and not roles[-1].get(field):
                        roles[-1][field] = extra[field]
        return roles

    def fetch_live_roles(self):
        """Reuse JobScanner's working Greenhouse/Lever/Ashby fetchers, plus any
        configured board-wide aggregators (Built In). No keyword scoring."""
        if self.max_live_roles <= 0:
            print("  skipping live board scan")
            return []
        config = self.scanner.load_sources()
        raw = []
        for company in config.get("companies", []):
            name = company["name"]
            api_url = company.get("api_url")
            if not api_url:
                continue
            if "greenhouse" in api_url:
                jobs = self.scanner.fetch_greenhouse_jobs(name, api_url)
            elif "lever" in api_url:
                jobs = self.scanner.fetch_lever_jobs(name, api_url)
            elif "ashbyhq" in api_url:
                jobs = self.scanner.fetch_ashby_jobs(name, api_url)
            else:
                continue
            for job in jobs:
                job["priority"] = company.get("priority")
            raw += jobs

        # Board-wide aggregators: no single company, so a separate config
        # section (queries/categories, not a per-company api_url).
        for agg in config.get("aggregators", []):
            if agg.get("provider") != "builtin":
                continue
            name = agg.get("name", "Built In")
            jobs = self.scanner.fetch_builtin_jobs(name, agg)
            for job in jobs:
                job["priority"] = agg.get("priority")
            raw += jobs

        print(f"  fetched {len(raw)} live postings")
        return raw

    def apply_auto_close(self, role, matched_pattern):
        """Deterministic 'verdict' for a title-heuristic match - no LLM call
        spent on something already decided. fit_fingerprint is still set, so
        this is cached exactly like any other verdict: a repeat sighting is a
        normal reuse, not a special case."""
        role["fit_fingerprint"] = self.fingerprint(role)
        role["fit_score"] = 0
        role["seniority_read"] = None
        role["why"] = (f'Auto-closed by title heuristic (matched "{matched_pattern}"). '
                        f"Edit rules.auto_close_titles in config/job-sources.yaml to change this.")
        role["top_gaps"] = []
        role["what_theyre_really_hiring_for"] = None
        role["pitch_angle"] = None
        role["recommendation"] = "skip"
        role["status"] = "04 Closed"

    def partition_auto_closed(self, live_roles):
        """Split fresh live finds into (auto_closed, kept). A title heuristic
        match is closed immediately with no LLM call; an existing tracker row
        is never passed to this - only ever fresh live finds are."""
        patterns = load_auto_close_titles(self.scanner)
        auto_closed, kept = [], []
        for role in live_roles:
            matched = matched_auto_close_title(role.get("title"), patterns)
            if matched:
                self.apply_auto_close(role, matched)
                auto_closed.append(role)
            else:
                kept.append(role)
        return auto_closed, kept

    def prefilter_live(self, raw, tracker_ids, tracker_urls=None):
        """Cheap, honest prefilter. Only shrinks the batch bill; no scoring claims.

        Reserves LOWER_TIER_RESERVE_FRACTION of max_live_roles for sources below
        AGGREGATOR_PRIORITY, so a high-volume aggregator can't take every slot on
        its own. Either side backfills the other's unused capacity, so the cap
        stays fully used whenever there are enough candidates somewhere.

        tracker_urls excludes by URL, not just by org+title slug. A board can
        retitle a posting without changing its id (Anthropic's greenhouse board
        showed one job as both "AAA, Commercial" and later "Applied AI
        Architect, Commercial"), and a URL match is the same real job either
        way. Without this, a retitled posting is judged fresh every run,
        forever fails to append (append_new_finds already refuses a URL it has
        seen), and is stuck as a ghost that never gets a store_id and is
        therefore never editable on the dashboard - it shows up as a second,
        permanently broken row for a job you already have tracked once.
        """
        tracker_urls = tracker_urls or set()
        seen, kept = set(), []
        for job in raw:
            title = (job.get("title") or "").strip()
            org = job.get("company") or "Unknown"
            if not title or not TITLE_PATTERN.search(title):
                continue
            url = _clean(job.get("url"))
            if url and url in tracker_urls:
                continue
            rid = _slug(org, title)
            if rid in seen or rid in tracker_ids:
                continue
            seen.add(rid)
            tier = 1 if TIER_1.search(title) else (2 if TIER_2.search(title) else 3)
            kept.append((tier, job.get("priority") or 5, rid, job))

        kept.sort(key=lambda item: (item[0], item[1], item[2]))

        aggregator_pool = [c for c in kept if c[1] <= AGGREGATOR_PRIORITY]
        lower_pool = [c for c in kept if c[1] > AGGREGATOR_PRIORITY]

        lower_quota = round(self.max_live_roles * LOWER_TIER_RESERVE_FRACTION)
        chosen_lower = lower_pool[:lower_quota]
        chosen_agg = aggregator_pool[: self.max_live_roles - len(chosen_lower)]

        # Either side backfills the other's shortfall so the cap stays full.
        remaining = self.max_live_roles - len(chosen_lower) - len(chosen_agg)
        if remaining > 0:
            extra = aggregator_pool[len(chosen_agg): len(chosen_agg) + remaining]
            chosen_agg += extra
            remaining -= len(extra)
        if remaining > 0:
            extra = lower_pool[len(chosen_lower): len(chosen_lower) + remaining]
            chosen_lower += extra
            remaining -= len(extra)

        kept = chosen_lower + chosen_agg
        kept.sort(key=lambda item: (item[0], item[1], item[2]))
        kept = kept[: self.max_live_roles]

        roles = []
        for tier, _prio, rid, job in kept:
            roles.append({
                "id": rid,
                "row_index": None,
                "source": "live",
                "org": job.get("company"),
                "title": job.get("title", "").strip(),
                "url": _clean(job.get("url")),
                "location": _clean(job.get("location")),
                "role_cat": None,
                "priority": job.get("priority"),
                "status": "01 Open",
                "date_opened": None,
                "date_applied": None,
                "outcome": None,
                "outcome_label": None,
                "days_to_outcome": None,
                "outcome_display": None,
                "notes": None,
                "comp_range": _clean(job.get("comp_range")),
                "jd": _strip_html(job.get("description")),
                "_tier": tier,
            })
        print(f"  prefiltered to {len(roles)} live roles for judgment")
        return roles

    # ---------------- fit analysis ----------------

    @staticmethod
    def _role_block(role):
        """The exact text the judge sees for one role.

        NOT the same text used for the reuse fingerprint - see
        _fingerprint_block. Notes and comp_range (Salary) are both editable
        from the dashboard now, and neither should force a fresh, paid
        re-judgment just because the user fixed a salary format or added a
        personal reminder; they still appear in what the model reads, since
        they can be genuinely useful context, but a change to either alone no
        longer busts the cache.
        """
        block = [f"[{role['id']}]", f"ORG: {role['org']}", f"TITLE: {role['title']}"]
        if role.get("location"):
            block.append(f"LOCATION: {role['location']}")
        if role.get("comp_range"):
            block.append(f"POSTED RANGE (k): {role['comp_range']}")
        if role.get("notes"):
            block.append(f"CANDIDATE NOTES: {role['notes'][:300]}")
        if role.get("jd"):
            block.append(f"JD EXCERPT: {role['jd']}")
        return "\n".join(block)

    @staticmethod
    def _fingerprint_block(role):
        """Subset of _role_block used ONLY for the reuse fingerprint.

        Deliberately excludes Notes and comp_range (Salary): both are
        dashboard-editable, and a personal note or a salary-format cleanup
        should not force a fresh, paid re-judgment. Everything that actually
        changes what the model is asked to evaluate - org, title, location,
        JD - still busts the cache exactly as before.
        """
        block = [f"[{role['id']}]", f"ORG: {role['org']}", f"TITLE: {role['title']}"]
        if role.get("location"):
            block.append(f"LOCATION: {role['location']}")
        if role.get("jd"):
            block.append(f"JD EXCERPT: {role['jd']}")
        return "\n".join(block)

    @classmethod
    def _prompt_signature(cls):
        """Hash of the fit instruction text and ground-truth context.

        Derived from the source itself, so editing the prompt automatically
        invalidates stored verdicts. PROMPT_VERSION stays as a manual override.
        """
        cached = getattr(cls, "_prompt_sig_cache", None)
        if cached:
            return cached
        import inspect as _inspect
        try:
            body = _inspect.getsource(cls._fit_prompt)
        except (OSError, TypeError):  # pragma: no cover - source unavailable
            body = "unknown"
        sig = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
        cls._prompt_sig_cache = sig
        return sig

    @classmethod
    def fingerprint(cls, role):
        """Hash of everything the fit verdict depends on.

        Covers the role content, the prompt version, and the prompt text
        itself - but NOT Notes or comp_range (Salary); see _fingerprint_block.
        """
        raw = f"v{PROMPT_VERSION}|{cls._prompt_signature()}\n{cls._fingerprint_block(role)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _fit_prefix(self):
        """The stable half of the fit prompt: candidate context, calibration
        bands, honesty rules and the required output shape. Byte-identical for
        every batch in a run, which is what lets the Anthropic backend cache it
        and makes small batches affordable.

        The output schema sits here, BEFORE the roles, so everything variable is
        strictly at the end - a cache prefix has to be a prefix."""
        return f"""{self.ctx}

=== TASK: HONEST FIT ANALYSIS ===
You are the candidate's blunt career advisor. Judge each role below on real fit for
THIS candidate, not on keyword overlap. The system you are replacing scored by
substring match and gave a junior "Measurement Partner II" and a "Staff Data Scientist"
the same 55. That was useless. Use judgment instead.

CALIBRATION - this is the required standard:
  fit 35 | "Currently a people-manager; Pinterest Staff DS is a senior IC role, a
            different track and likely a step down in title."
Blunt, specific, calibrated.

SCORING BANDS (obey them):
  85-100  rare. Right level, right track, right domain. Should already be applying.
  70-84   strong. Real match with one fixable gap.
  50-69   plausible but compromised: level, track, or domain is off.
  25-49   weak. Wrong track, wrong level, or only surface keyword overlap.
  0-24    not a fit at all.
Most roles must land BELOW 50. A batch where many roles score high is a failed batch.
Do not grade on a curve and do not spread scores to look balanced.

THINGS THAT MATTER MORE THAN KEYWORDS:
  - IC vs people-manager track. A manager taking a senior IC seat is a track change,
    usually a step down. Say so plainly.
  - Level words: II / Partner / Associate are junior. Staff / Principal / Lead / Head /
    Director / VP are not interchangeable.
  - Function drift: ads sales, account management, recruiting research, pure ML
    infrastructure, and research-scientist roles are NOT measurement science.
  - Domain: measurement / causal / experimentation / MMM / incrementality is the core.

HONESTY RULES:
  - "skip" is a correct and welcome answer. Most roles should be "skip".
  - If it is a bad fit, set pitch_angle to null. Do not invent a pitch. Do not force it.
  - Do not pad top_gaps with filler. Concrete gaps only, from the JD and the resume.
  - Under-claiming beats over-claiming. An empty-handed honest verdict is a success.

Return ONLY a JSON array, one object per role, echoing the id exactly:
[
  {{
    "id": "<id from brackets>",
    "fit_score": <int 0-100>,
    "seniority_read": "<one line on level/track match or mismatch>",
    "why": "<2-3 sentences>",
    "top_gaps": ["<concrete gap>", "<concrete gap>"],
    "what_theyre_really_hiring_for": "<read between the lines of the JD>",
    "pitch_angle": "<one sentence to lead with, or null if bad fit>",
    "recommendation": "apply" | "research" | "skip"
  }}
]

ROLES follow. Judge every one of them, and echo each id exactly."""

    def _fit_roles(self, batch):
        """The variable half: just the roles under judgement."""
        roles_text = "\n\n".join(self._role_block(role) for role in batch)
        return f"ROLES:\n{roles_text}"

    def _fit_prompt(self, batch):
        """The whole prompt as one string, for callers and tests that want it."""
        return f"{self._fit_prefix()}\n\n{self._fit_roles(batch)}"

    def load_prior_fits(self):
        """Every fit verdict already on disk, newest file wins.

        Returns (by_fingerprint, by_id). Two tracker rows can legitimately share an
        id - same org and title, different notes or range - so the fingerprint index
        is what decides reuse. Keying reuse by id alone left such a row unmatched and
        re-judged at full price every single run. The id index is kept only as a
        fallback for when a fresh judgment fails.
        """
        by_fp, by_id = {}, {}
        files = sorted((self.base_dir / "artifacts/jobs").glob("intel-*.json"), reverse=True)
        for path in files:
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            for role in data.get("roles", []):
                rid, fp = role.get("id"), role.get("fit_fingerprint")
                if not fp or role.get("fit_score") is None:
                    continue
                by_fp.setdefault(fp, role)
                if rid:
                    by_id.setdefault(rid, role)
        return by_fp, by_id

    def split_by_freshness(self, roles, seed=None):
        """Return (needs_judging, reused_count).

        A role is reused only when its fingerprint matches a stored verdict, i.e.
        the org, title, location, posted range, notes, JD excerpt and prompt
        version are all unchanged. Anything new or edited goes back to the model.
        """
        by_fp, by_id = ({}, {}) if self.refresh else self.load_prior_fits()
        # Verdicts handed in by the caller (add_role.py just paid for one) win,
        # so the same role is never judged twice in a single sitting. Keyed by
        # fingerprint, same as everything else here.
        by_fp.update(seed or {})
        self._prior_by_id = by_id
        fresh, reused = [], 0
        for role in roles:
            fp = self.fingerprint(role)
            role["fit_fingerprint"] = fp
            hit = by_fp.get(fp)
            if hit and hit.get("fit_score") is not None:
                for field in FIT_FIELDS:
                    role[field] = hit.get(field)
                reused += 1
            else:
                fresh.append(role)
        return fresh, reused

    def analyze_fit(self, roles):
        batches = [roles[i:i + self.batch_size] for i in range(0, len(roles), self.batch_size)]
        results = {}
        for num, batch in enumerate(batches, 1):
            print(f"  fit batch {num}/{len(batches)} ({len(batch)} roles)...", flush=True)
            self._judge_batch(batch, results, label=str(num))
        return results

    def _judge_batch(self, batch, results, label=""):
        """Judge one batch, halving it on failure rather than losing the roles.

        A batch that fails as a whole - truncated output, or one malformed
        verdict failing validation for everybody - used to leave every role in
        it unjudged until the next run. Splitting turns that into a slower run
        instead of a gap, and isolates the one role that is actually causing it.
        """
        try:
            data = self.llm.complete_json(
                self._fit_roles(batch),
                tag=f"job-fit-{len(batch)}",
                validate=make_fit_validator(batch),
                cache_prefix=self._fit_prefix(),
                max_tokens=output_budget(len(batch)),
            )
        except LLMError as exc:
            if len(batch) > 1:
                half = len(batch) // 2
                print(f"    ! batch {label} failed ({exc}); splitting into "
                      f"{half} + {len(batch) - half}")
                self._judge_batch(batch[:half], results, label=f"{label}a")
                self._judge_batch(batch[half:], results, label=f"{label}b")
            else:
                print(f"    ! role {batch[0].get('id')} could not be judged: {exc}")
            return

        if isinstance(data, dict):
            data = data.get("roles") or data.get("results") or list(data.values())
        valid = {role["id"] for role in batch}
        for item in data or []:
            if not isinstance(item, dict):
                continue
            rid = item.get("id")
            if rid not in valid:
                continue
            results[rid] = item
        missing = valid - set(results)
        if missing:
            print(f"    ! {len(missing)} roles unjudged in batch {label}")

    @staticmethod
    def merge_fit(role, fit):
        def norm_score(value):
            try:
                return max(0, min(100, int(round(float(value)))))
            except (TypeError, ValueError):
                return None

        gaps = fit.get("top_gaps") if fit else None
        if isinstance(gaps, str):
            gaps = [gaps]
        pitch = (fit or {}).get("pitch_angle")
        if isinstance(pitch, str) and pitch.strip().lower() in {"null", "none", "n/a", ""}:
            pitch = None
        rec = (fit or {}).get("recommendation")
        rec = rec.lower().strip() if isinstance(rec, str) else None
        if rec not in {"apply", "research", "skip"}:
            rec = None

        role.update({
            "fit_score": norm_score((fit or {}).get("fit_score")),
            "seniority_read": _clean((fit or {}).get("seniority_read")),
            "why": _clean((fit or {}).get("why")),
            "top_gaps": [g for g in (gaps or []) if isinstance(g, str) and g.strip()][:3],
            "what_theyre_really_hiring_for": _clean((fit or {}).get("what_theyre_really_hiring_for")),
            "pitch_angle": _clean(pitch),
            "recommendation": rec,
        })
        return role

    # ---------------- role categorisation ----------------

    def load_cat_cache(self):
        """Stored archetype suggestions, keyed by role id.

        {role_id: {"fingerprint": str, "suggested_role_cat": str|None,
                   "suggestion_confidence": str|None, "suggestion_reason": str|None}}
        """
        path = self.base_dir / "data/role-categories.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def save_cat_cache(self, cache):
        path = self.base_dir / "data/role-categories.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2, sort_keys=True))

    @staticmethod
    def _cat_block(role):
        """The exact text the categoriser sees for one role."""
        block = [f"[{role['id']}]", f"ORG: {role['org']}", f"TITLE: {role['title']}"]
        if role.get("jd"):
            block.append(f"JD EXCERPT: {role['jd'][:400]}")
        return "\n".join(block)

    @classmethod
    def cat_fingerprint(cls, role, archetypes):
        """Hash of everything a category suggestion depends on."""
        names = "|".join(f"{a['id']}:{a['label']}" for a in archetypes)
        raw = f"v{CAT_PROMPT_VERSION}|{names}\n{cls._cat_block(role)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _cat_prompt(self, batch, archetypes):
        listed = "\n".join(f'  {a["id"]} = {a["label"]} ({a["description"]})' for a in archetypes)
        roles_text = "\n\n".join(self._cat_block(role) for role in batch)
        return f"""You are sorting job postings into a candidate's own role archetypes.

THE ARCHETYPES (the only valid ids besides "none"):
{listed}

RULES:
  - Pick the ONE archetype the posting genuinely belongs to, judged on what the job
    actually is, not on shared keywords.
  - "none" is a correct and expected answer. Many real postings do not map to any of
    these archetypes: wrong function, wrong level, or a different discipline entirely.
    Returning "none" is better than a forced match. Do not spread answers to fill all
    six buckets, and do not invent a fit.
  - confidence is "high" only when the title and content make the archetype obvious;
    "medium" when it is a reasonable read; "low" when you are largely guessing. If you
    would say "low" and the mapping is thin, return "none" instead.
  - reason is ONE short clause, concrete, drawn from the title or JD. No filler.

ROLES:
{roles_text}

Return ONLY a JSON array, one object per role, echoing the id exactly:
[
  {{"id": "<id from brackets>", "archetype": "01".."0{len(archetypes)}" | "none",
    "confidence": "high" | "medium" | "low", "reason": "<one short clause>"}}
]"""

    def categorize_roles(self, roles, archetypes):
        """Suggest an archetype for every role with no user-set Role Cat.

        One batched call for everything not already cached under the same
        fingerprint. Returns the number of roles sent to the model.
        """
        if not archetypes:
            print("  categorisation: no archetypes in me/profile.md, skipped")
            return 0

        cache = self.load_cat_cache()
        todo = []
        for role in roles:
            if _clean(role.get("role_cat")):
                continue  # user already decided; never second-guess it
            fp = self.cat_fingerprint(role, archetypes)
            role["cat_fingerprint"] = fp
            hit = cache.get(role["id"])
            if hit and hit.get("fingerprint") == fp and not self.refresh:
                for field in CAT_FIELDS:
                    role[field] = hit.get(field)
            else:
                todo.append(role)

        made_this_run = 0
        if todo:
            made_this_run = self._judge_categories(todo, archetypes, cache)

        # Auto-accept: the model's determination IS the Role Cat now, not a
        # proposal waiting for a click. Still fully editable afterward from the
        # dashboard - this only ever fires once per role, since a role with
        # Role Cat already set is skipped at the top of this function on every
        # future run, exactly like a category the user typed in by hand.
        # Covers roles reused from cache too (an old suggestion made before
        # this existed gets swept up here, no LLM call spent doing it).
        accepted = [role for role in roles
                    if not _clean(role.get("role_cat")) and role.get("suggested_role_cat")]
        for role in accepted:
            role["role_cat"] = role["suggested_role_cat"]
        if accepted:
            print(f"  auto-accepted {len(accepted)} suggested categor"
                  f"{'y' if len(accepted) == 1 else 'ies'}")

        if not todo:
            print("  categorisation: all uncategorised roles cached")
        return made_this_run

    def _judge_categories(self, todo, archetypes, cache):
        """The LLM half of categorize_roles, in chunks.

        This used to be a single call for every uncategorised role at once. At
        315 roles that overflowed the model's output budget and was cut off
        mid-answer, losing every suggestion in the run. Chunking keeps each
        answer well inside the budget, and a chunk that still fails is split
        rather than abandoned."""
        batches = [todo[i:i + CAT_BATCH_SIZE] for i in range(0, len(todo), CAT_BATCH_SIZE)]
        print(f"  categorising {len(todo)} uncategorised role(s) "
              f"in {len(batches)} call(s)...", flush=True)
        valid_ids = {a["id"] for a in archetypes}
        by_id = {a["id"]: a["label"] for a in archetypes}

        verdicts = {}
        for num, chunk in enumerate(batches, 1):
            self._judge_cat_chunk(chunk, archetypes, valid_ids, verdicts, label=str(num))

        for role in todo:
            item = verdicts.get(role["id"]) or {}
            archetype = _clean(item.get("archetype")) or "none"
            confidence = (_clean(item.get("confidence")) or "").lower()
            if archetype not in valid_ids or confidence not in {"high", "medium", "low"}:
                # Unusable answer, including an honest "none": no suggestion.
                role["suggested_role_cat"] = None
                role["suggestion_confidence"] = None
                role["suggestion_reason"] = None
            else:
                role["suggested_role_cat"] = by_id[archetype]
                role["suggestion_confidence"] = confidence
                role["suggestion_reason"] = _clean(item.get("reason"))
            cache[role["id"]] = {
                "fingerprint": role.get("cat_fingerprint"),
                **{f: role.get(f) for f in CAT_FIELDS},
            }

        self.save_cat_cache(cache)
        made = sum(1 for r in todo if r.get("suggested_role_cat"))
        print(f"  categorisation: {made} suggestion(s), {len(todo) - made} left as 'none'")
        return len(todo)

    def _judge_cat_chunk(self, chunk, archetypes, valid_ids, verdicts, label=""):
        """One categorisation call, halving the chunk if it fails."""
        try:
            data = self.llm.complete_json(
                self._cat_prompt(chunk, archetypes),
                tag=f"role-cat-{len(chunk)}",
                validate=make_cat_validator(chunk, valid_ids),
                max_tokens=output_budget(len(chunk), per_item=CAT_TOKENS_PER_ROLE),
            )
        except LLMError as exc:
            if len(chunk) > 1:
                half = len(chunk) // 2
                print(f"    ! categorisation batch {label} failed ({exc}); splitting into "
                      f"{half} + {len(chunk) - half}")
                self._judge_cat_chunk(chunk[:half], archetypes, valid_ids, verdicts, f"{label}a")
                self._judge_cat_chunk(chunk[half:], archetypes, valid_ids, verdicts, f"{label}b")
            else:
                print(f"    ! could not categorise {chunk[0].get('id')}: {exc}")
            return

        if isinstance(data, dict):
            data = data.get("roles") or data.get("results") or list(data.values())
        wanted = {r["id"] for r in chunk}
        for item in data or []:
            if isinstance(item, dict) and item.get("id") in wanted:
                verdicts[item["id"]] = item


    def load_jd_cache(self):
        """Descriptions for manually added roles, keyed by role id.

        The tracker has no description column, so tracker roles are normally judged
        on title and notes alone. A role added by URL keeps its JD here, which makes
        its verdict materially better and survives across runs.
        """
        if self._jd_cache is None:
            path = self.base_dir / "data/job-descriptions.json"
            try:
                self._jd_cache = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                self._jd_cache = {}
        return self._jd_cache

    def save_jd_cache(self):
        path = self.base_dir / "data/job-descriptions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._jd_cache or {}, indent=2, sort_keys=True))

    def add_role(self, url, org=None, title=None, jd_limit=1400):
        """Add one posting to the tracker from a public URL.

        Returns (role_id, message). role_id is None when nothing was added.
        Extraction is deterministic; --org/--title override whatever was scraped.
        """
        from job_fetch import fetch_posting

        print(f"  fetching {url}")
        posting = fetch_posting(url)
        if posting.get("error") and not (posting.get("title") or title):
            return None, f"could not read the posting: {posting['error']}"

        org = _clean(org) or _clean(posting.get("org"))
        title = _clean(title) or _clean(posting.get("title"))
        if not org or not title:
            missing = ", ".join(n for n, v in (("org", org), ("title", title)) if not v)
            # Lever and Ashby never expose a company display name. Offer the URL
            # slug as a hint for the human to confirm; do not adopt it silently.
            slug = re.search(r"(?:lever\.co|ashbyhq\.com|greenhouse\.io)/([^/?#]+)", url)
            hint = f" The URL slug is '{slug.group(1)}'." if slug and not org else ""
            return None, (f"extraction did not find: {missing}.{hint} "
                          f"Re-run with --org/--title to set it by hand.")

        rid = _slug(org, title)
        df = self.load_tracker()
        if not df.empty:
            existing = {_slug(_clean(r.get("Org")) or "Unknown", _clean(r.get("Title")) or "Untitled")
                        for _i, r in df.iterrows()}
            if rid in existing:
                return None, f"already tracked: {org} - {title}"

        cache = self.load_jd_cache()
        cache[rid] = {
            "url": _clean(posting.get("url")) or url,
            "location": _clean(posting.get("location")),
            "comp_range": _clean(posting.get("comp_range")),
            "jd": _strip_html(posting.get("jd") or "", limit=jd_limit) or None,
            "source": posting.get("source"),
            "added": datetime.now().strftime("%Y-%m-%d"),
        }
        self.save_jd_cache()

        today = datetime.now().strftime("%Y-%m-%d")
        row = {
            "Org": org,
            "Title": title,
            "Role Link": cache[rid]["url"],
            "Source": f"manual ({posting.get('source')})",
            "Status": "01 Open",
            "Date Opened": today,
            "Last Updated": today,
            "Range": cache[rid]["comp_range"],
        }

        if df.empty:
            df = pd.DataFrame([row])
        else:
            self.backup_tracker()
            before = len(df)
            df = pd.concat([df, pd.DataFrame([{c: row.get(c) for c in df.columns}])],
                           ignore_index=True)
            assert len(df) == before + 1, "append did not add exactly one row"
        self.save_tracker(df, actor="add_role")

        got = [k for k in ("location", "comp_range", "jd") if cache[rid].get(k)]
        return rid, (f"added {org} - {title} (via {posting.get('source')}; "
                     f"captured {', '.join(got) or 'title only'})")

    def load_sector_cache(self):
        if self.sector_cache_path.exists():
            try:
                return json.loads(self.sector_cache_path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def enrich_orgs(self, orgs):
        cache = self.load_sector_cache()
        missing = sorted({o for o in orgs if o and o not in cache})
        if missing:
            listed = "\n".join(f"- {o}" for o in missing)
            prompt = f"""Label each company below with a short two-part sector line for a
job-tracker UI. Format exactly: "<Industry> - <Niche>".

Examples of the required style:
  Google            -> Tech - Advertising
  Anthropic         -> Tech - AI Lab
  Zocdoc            -> Healthcare - Health Tech
  Gusto             -> Tech - HR & Payroll SaaS
  Chewy             -> Retail - E-commerce
  Horizon Media     -> Media - Media Agency

Rules:
  - Keep each side to 1-3 words. No sentences, no hedging, no punctuation beyond "&".
  - If you genuinely do not recognise the company, return "Unknown - Unknown".
    Guessing wrong is worse than admitting you do not know. Do not force a label.

COMPANIES:
{listed}

Return ONLY a JSON object mapping each company name exactly as given to its sector line:
{{"Company Name": "Industry - Niche"}}"""
            print(f"  sector enrichment for {len(missing)} new orgs...", flush=True)
            try:
                data = self.llm.complete_json(prompt, tag="org-sectors",
                                              validate=make_sector_validator(missing))
                if isinstance(data, dict):
                    for org, sector in data.items():
                        if isinstance(sector, str) and sector.strip():
                            cache[org] = sector.strip()
            except LLMError as exc:
                print(f"    ! sector enrichment failed: {exc}")
        else:
            print("  sectors: all orgs cached")

        self.sector_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.sector_cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True))
        return cache

    # ---------------- org directory ----------------

    @staticmethod
    def build_org_directory(roles, sectors):
        by_org = {}
        for role in roles:
            by_org.setdefault(role["org"], []).append(role)

        directory = []
        for org, items in by_org.items():
            statuses, recs = {}, {"apply": 0, "research": 0, "skip": 0}
            for role in items:
                status = role.get("status") or "Unknown"
                statuses[status] = statuses.get(status, 0) + 1
                if role.get("recommendation") in recs:
                    recs[role["recommendation"]] += 1

            scores = [r["fit_score"] for r in items if isinstance(r.get("fit_score"), int)]
            best = max((r for r in items if isinstance(r.get("fit_score"), int)),
                       key=lambda r: r["fit_score"], default=None)
            outcomes = [
                {"title": r["title"], "outcome": r["outcome"],
                 "days_to_outcome": r["days_to_outcome"]}
                for r in items if r.get("outcome")
            ]
            directory.append({
                "org": org,
                "sector": sectors.get(org),
                "role_count": len(items),
                "open_count": sum(1 for r in items if (r.get("status") or "").startswith("01")),
                "applied_count": sum(1 for r in items if (r.get("status") or "").startswith("03")),
                "closed_count": sum(1 for r in items if (r.get("status") or "").startswith("04")),
                "status_mix": dict(sorted(statuses.items())),
                "recommendation_mix": recs,
                "best_fit_score": max(scores) if scores else None,
                "median_fit_score": int(round(statistics.median(scores))) if scores else None,
                "best_role": ({"title": best["title"], "fit_score": best["fit_score"],
                               "url": best.get("url")} if best else None),
                "outcomes": outcomes,
            })
        directory.sort(key=lambda o: (-(o["best_fit_score"] or -1), o["org"]))
        return directory

    # ---------------- tracker update ----------------

    def backup_tracker(self):
        """Back up the record itself - the database - before this run writes to it."""
        dest = self.store.backup_db()
        print(f"  backup -> {dest.name}")
        return dest

    def append_new_finds(self, df, roles):
        """Append live-scan roles that are not in the tracker yet.

        The tracker is the record, so anything the pipeline has seen belongs in
        it - not only the rows typed by hand. New rows land as
        NEW_FIND_STATUS so they are obviously machine-added and can be filtered
        out; every manual column except Status is left blank for the user.

        Returns (df, number_appended). Mutates each appended role's row_index so
        update_tracker fills its intel columns in the same pass.
        """
        if not AUTO_ADD_LIVE:
            return df, 0

        known_ids = {
            _slug(_clean(row.get("Org")) or "Unknown", _clean(row.get("Title")) or "Untitled")
            for _, row in df.iterrows()
        }
        known_urls = {
            _clean(row.get("Role Link")) for _, row in df.iterrows() if _clean(row.get("Role Link"))
        }

        pending = []
        for role in roles:
            if role.get("row_index") is not None:
                continue
            url = _clean(role.get("url"))
            if role["id"] in known_ids or (url and url in known_urls):
                continue
            known_ids.add(role["id"])
            if url:
                known_urls.add(url)
            pending.append(role)

        if not pending:
            return df, 0

        today = datetime.now().strftime("%Y-%m-%d")
        cache = self.load_jd_cache()
        rows = []
        for role in pending:
            # Keep the scan's location / range / JD in the jd cache under the same
            # id. tracker_roles reads it back, so the text the judge sees is
            # identical before and after the row is appended - without this the
            # fingerprint changes and every appended role is re-judged at full
            # price on the next run. Notes stays empty: it belongs to the user.
            cache.setdefault(role["id"], {
                "url": _clean(role.get("url")),
                "location": _clean(role.get("location")),
                "comp_range": _clean(role.get("comp_range")),
                "jd": role.get("jd") or None,
                "source": role.get("source") or "live",
                "added": today,
            })
            row = {column: pd.NA for column in df.columns}
            row.update({
                "Org": role.get("org"),
                "Title": role.get("title"),
                # A title-heuristic match already decided "04 Closed" (see
                # apply_auto_close); anything else gets the normal new-find
                # status regardless of whatever prefilter_live defaulted it to.
                "Status": "04 Closed" if role.get("status") == "04 Closed" else NEW_FIND_STATUS,
                "Source": role.get("source") or "live",
                "Role Link": _clean(role.get("url")),
                "Date Opened": today,
                "Last Updated": today,
            })
            if _clean(role.get("comp_range")):
                row["Range"] = _clean(role.get("comp_range"))
            rows.append(row)
        self.save_jd_cache()

        start = len(df)
        df = pd.concat([df, pd.DataFrame(rows, columns=df.columns)], ignore_index=True)
        for offset, role in enumerate(pending):
            role["row_index"] = start + offset
            # Mirror the tracker row's own Status exactly (see above): a
            # title-heuristic close must survive on the role dict too, since
            # that is what gets serialized into today's intel.json.
            role["status"] = "04 Closed" if role.get("status") == "04 Closed" else NEW_FIND_STATUS

        closed_count = sum(1 for role in pending if role["status"] == "04 Closed")
        if closed_count:
            print(f"  appended {len(rows)} new find(s) to the tracker "
                  f"({closed_count} as '04 Closed', {len(rows) - closed_count} as '{NEW_FIND_STATUS}')")
        else:
            print(f"  appended {len(rows)} new find(s) to the tracker as '{NEW_FIND_STATUS}'")
        return df, len(rows)

    def update_tracker(self, df, roles):
        """Add the 5 intel columns. Existing values and manual columns are untouched."""
        before_shape = df.shape
        before = df.copy(deep=True)

        by_index = {r["row_index"]: r for r in roles if r.get("row_index") is not None}
        for column in NEW_COLUMNS:
            if column not in df.columns:
                df[column] = pd.NA

        for idx, role in by_index.items():
            gaps = role.get("top_gaps") or []
            rationale_bits = [b for b in [role.get("seniority_read"), role.get("why")] if b]
            if gaps:
                rationale_bits.append("Gaps: " + "; ".join(gaps))
            rationale = " ".join(rationale_bits).strip()

            if role.get("fit_score") is not None:
                df.at[idx, "Fit Score"] = role["fit_score"]
            if rationale:
                df.at[idx, "Fit Rationale"] = rationale
            if role.get("recommendation"):
                df.at[idx, "Recommendation"] = role["recommendation"]
            if role.get("sector"):
                df.at[idx, "Sector"] = role["sector"]
            if role.get("days_to_outcome") is not None:
                df.at[idx, "Days To Outcome"] = role["days_to_outcome"]

            # Role Cat is auto-accepted from the model's suggestion (see
            # categorize_roles) whenever the cell is still blank - write it
            # once, here. A cell that already has a value, from a user or from
            # a prior auto-accept, is never touched again.
            existing_cat = (_clean(before.at[idx, "Role Cat"])
                            if idx < before_shape[0] and "Role Cat" in before.columns else None)
            if role.get("role_cat") and not existing_cat:
                df.at[idx, "Role Cat"] = role["role_cat"]

            # Suggestion column is now mostly vestigial - auto-accept means a
            # real suggestion almost never sits unaccepted - but still clears
            # correctly whenever Role Cat ends up set.
            if _clean(role.get("role_cat")):
                df.at[idx, SUGGESTED_CAT_COLUMN] = pd.NA
            elif role.get("suggested_role_cat"):
                confidence = role.get("suggestion_confidence") or "?"
                df.at[idx, SUGGESTED_CAT_COLUMN] = f"{role['suggested_role_cat']} ({confidence})"

        # Safety: rows may be APPENDED, never lost or reordered, and the manual
        # columns of rows that already existed must come out byte-identical -
        # with one deliberate exception: Role Cat may move from blank to a
        # value (auto-accept), but a cell that already held a value must still
        # come out exactly as it was.
        assert len(df) >= before_shape[0], "rows were lost"
        kept = df.iloc[:before_shape[0]]
        for column in MANUAL_COLUMNS:
            if column not in before.columns:
                continue
            if column == "Role Cat":
                for i in range(before_shape[0]):
                    was_blank = not _clean(before.iloc[i][column])
                    if not was_blank:
                        assert str(before.iloc[i][column]) == str(kept.iloc[i][column]), \
                            f"manual column mutated: Role Cat (row {i} already had a value)"
                continue
            same = before[column].astype(str).equals(kept[column].astype(str))
            assert same, f"manual column mutated: {column}"

        summary = self.save_tracker(df)
        print(f"  tracker {before_shape} -> {df.shape} "
              f"(db: +{summary['inserted']} rows, {summary['fields']} field(s) changed)")
        return df

    # ---------------- run ----------------

    def run(self, seed_fits=None):
        """seed_fits: {role_id: role dict with fit_fingerprint + fit fields} to reuse."""
        print("Running Job Intel...")
        df = self.load_tracker()
        tracked = self.tracker_roles(df)
        print(f"  tracker: {len(tracked)} roles")

        tracked_urls = {r["url"] for r in tracked if r.get("url")}
        live = self.prefilter_live(self.fetch_live_roles(), {r["id"] for r in tracked}, tracked_urls)
        auto_closed, live_kept = self.partition_auto_closed(live)
        if auto_closed:
            print(f"  auto-closed {len(auto_closed)} live role(s) by title heuristic, no LLM call")

        judged_pool = tracked + live_kept
        fresh, reused = self.split_by_freshness(judged_pool, seed=seed_fits)
        if reused:
            print(f"  reusing {reused} unchanged verdict(s); judging {len(fresh)} new or edited")
            for role in fresh[:5]:
                print(f"    + {role['org']} - {role['title']}")
            if len(fresh) > 5:
                print(f"    + ... {len(fresh) - 5} more")
        if fresh:
            fits = self.analyze_fit(fresh)
            recovered = 0
            for role in fresh:
                fit = fits.get(role["id"])
                if fit:
                    self.merge_fit(role, fit)
                    continue
                # The batch failed or the model skipped this role. Keep the last
                # known verdict rather than blanking the page with nulls.
                stale = (getattr(self, "_prior_by_id", None) or {}).get(role["id"])
                if stale:
                    for field in FIT_FIELDS:
                        role[field] = stale.get(field)
                    role["fit_stale"] = True
                    recovered += 1
                else:
                    self.merge_fit(role, {})
            if recovered:
                print(f"  kept {recovered} previous verdict(s) where this run returned nothing")
        else:
            print("  nothing new to judge")

        roles = judged_pool + auto_closed

        for role in roles:
            for field in CAT_FIELDS:
                role.setdefault(field, None)
        self.categorize_roles(roles, load_archetypes(self.base_dir, df))

        sectors = self.enrich_orgs({r["org"] for r in roles})
        for role in roles:
            role["sector"] = sectors.get(role["org"])

        # Runs even when the tracker is empty: that is exactly the case where a
        # new workspace needs its first rows written.
        if df is not None:
            self.backup_tracker()
            df, _added = self.append_new_finds(df, roles)
            self.update_tracker(df, roles)

            # append_new_finds could not know the store's assigned id for a
            # brand-new row - that assignment happens inside save_tracker, one
            # call later. Without this backfill, a role appended THIS run has
            # no store_id in today's intel.json, so the dashboard has nothing
            # to address it by and silently renders it without any of the
            # edit controls - it would only become editable starting with the
            # NEXT day's render, once tracker_roles() re-reads the store.
            if _added:
                store_df = self.store.to_df()
                for role in roles:
                    if role.get("store_id") or role.get("row_index") is None:
                        continue
                    try:
                        role["store_id"] = _clean(store_df.iloc[role["row_index"]]["_id"])
                    except IndexError:
                        pass

        for role in roles:
            role.pop("_tier", None)
            role.pop("jd", None)
            role.pop("row_index", None)

        roles.sort(key=lambda r: (-(r.get("fit_score") or -1), r["org"], r["title"]))
        orgs = self.build_org_directory(roles, sectors)

        today = datetime.now().strftime("%Y-%m-%d")
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "date": today,
            "counts": {
                "roles": len(roles),
                "tracker_roles": len(tracked),
                "live_roles": len(live),
                "judged_this_run": len(fresh),
                "reused_verdicts": reused,
                "orgs": len(orgs),
                "uncategorised": sum(1 for r in roles if not r.get("role_cat")),
                "suggested_categories": sum(1 for r in roles if r.get("suggested_role_cat")),
                "apply": sum(1 for r in roles if r.get("recommendation") == "apply"),
                "research": sum(1 for r in roles if r.get("recommendation") == "research"),
                "skip": sum(1 for r in roles if r.get("recommendation") == "skip"),
            },
            "llm": self.llm.report(),
            "roles": roles,
            "orgs": orgs,
        }

        out_path = self.base_dir / f"artifacts/jobs/intel-{today}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2))
        print(f"  wrote {out_path}")

        # A plain-text copy of the tracker, rotated like the database backups.
        # Readable without this tool, or any tool, if it is ever needed.
        if not self.store.is_empty():
            csv_path = self.store.export_csv()
            print(f"  csv -> {csv_path.name}")

        print(f"  {self.llm.report()}")
        return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Honest LLM fit analysis for tracked and live roles.")
    ap.add_argument("--refresh", action="store_true",
                    help="re-judge every role, ignoring stored verdicts (costs full price)")
    ap.add_argument("--max-live", type=int, default=None,
                     help="cap on live postings judged (default: rules.max_live_roles in job-sources.yaml, else 200)")
    ap.add_argument("--add", metavar="URL",
                    help="add one posting from a public URL, then judge it")
    ap.add_argument("--org", help="override the org name when using --add")
    ap.add_argument("--title", help="override the role title when using --add")
    ap.add_argument("--no-scan", action="store_true",
                    help="with --add: judge only the new role, skip the live board scan")
    args = ap.parse_args(argv)

    intel = JobIntel(Path(__file__).parent.parent,
                     max_live_roles=0 if (args.add and args.no_scan) else args.max_live,
                     refresh=args.refresh)

    if args.add:
        rid, message = intel.add_role(args.add, org=args.org, title=args.title)
        print(f"  {message}")
        if not rid:
            return 1

    intel.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

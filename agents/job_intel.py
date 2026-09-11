"""
Job Intelligence Agent (daily).

Replaces the substring keyword scoring in job_scanner.py with real LLM judgment.
Fetching stays where it works: JobScanner.fetch_greenhouse_jobs / fetch_lever_jobs.

WHAT IT DOES
  1. FIT ANALYSIS   - every tracker role + a prefiltered slice of live roles is judged
                      by the LLM in batches (~16 roles per call), against the candidate
                      context block (career-goals.md + resume).
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

from llm import LLM, LLMError            # noqa: E402
from context import context_block        # noqa: E402
from job_scanner import JobScanner       # noqa: E402


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

# Tracker backups kept on disk, newest first. One is written per run.
KEEP_BACKUPS = 7

# Only prune backups this agent made. Anything else in that folder is left alone.
BACKUP_RE = re.compile(r"^org-roles-tracker-backup-\d{8}-\d{6}\.xlsx$")

# Written by merge_fit, carried forward verbatim when a role is unchanged.
FIT_FIELDS = [
    "fit_score", "seniority_read", "why", "top_gaps",
    "what_theyre_really_hiring_for", "pitch_angle", "recommendation",
]
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
# role archetypes (career-goals.md "## Target Roles")
# --------------------------------------------------------------------------

_TARGET_ROLE_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")


def load_archetypes(base_dir, tracker_df=None):
    """Return [{"id": "01", "label": ..., "description": ...}] from career-goals.md.

    The numbered list under "## Target Roles" is the source of truth for the set.
    Where the user already uses a label in the tracker's Role Cat column for the
    same number, that exact label wins, so suggestions read like the user's own
    vocabulary instead of a paraphrase.
    """
    path = Path(base_dir) / "config/career-goals.md"
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

class JobIntel:
    def __init__(self, base_dir, batch_size=16, max_live_roles=45, model=None, refresh=False):
        self.refresh = refresh
        self._jd_cache = None
        self.base_dir = Path(base_dir)
        self.tracker_path = self.base_dir / "artifacts/jobs/org-roles-tracker.xlsx"
        self.sector_cache_path = self.base_dir / "data/org-sectors.json"
        self.batch_size = batch_size
        self.max_live_roles = max_live_roles
        self.llm = LLM(self.base_dir, model=model)
        self.scanner = JobScanner(self.base_dir)
        self.ctx = context_block(self.base_dir)

    # ---------------- role universe ----------------

    def load_tracker(self):
        if not self.tracker_path.exists():
            return pd.DataFrame()
        return pd.read_excel(self.tracker_path)

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
        """Reuse JobScanner's working Greenhouse/Lever fetchers. No keyword scoring."""
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
            else:
                continue
            for job in jobs:
                job["priority"] = company.get("priority")
            raw += jobs
        print(f"  fetched {len(raw)} live postings")
        return raw

    def prefilter_live(self, raw, tracker_ids):
        """Cheap, honest prefilter. Only shrinks the batch bill; no scoring claims."""
        seen, kept = set(), []
        for job in raw:
            title = (job.get("title") or "").strip()
            org = job.get("company") or "Unknown"
            if not title or not TITLE_PATTERN.search(title):
                continue
            rid = _slug(org, title)
            if rid in seen or rid in tracker_ids:
                continue
            seen.add(rid)
            tier = 1 if TIER_1.search(title) else (2 if TIER_2.search(title) else 3)
            kept.append((tier, job.get("priority") or 5, rid, job))

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
                "comp_range": None,
                "jd": _strip_html(job.get("description")),
                "_tier": tier,
            })
        print(f"  prefiltered to {len(roles)} live roles for judgment")
        return roles

    # ---------------- fit analysis ----------------

    @staticmethod
    def _role_block(role):
        """The exact text the judge sees for one role.

        Also the basis of the reuse fingerprint, so the two can never drift:
        if what the model reads is identical, the verdict is reusable.
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

        Covers the role content, the prompt version, and the prompt text itself.
        """
        raw = f"v{PROMPT_VERSION}|{cls._prompt_signature()}\n{cls._role_block(role)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _fit_prompt(self, batch):
        roles_text = "\n\n".join(self._role_block(role) for role in batch)

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

ROLES:
{roles_text}

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
]"""

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
            try:
                data = self.llm.complete_json(self._fit_prompt(batch), tag=f"job-fit-{len(batch)}")
            except LLMError as exc:
                print(f"    ! batch {num} failed: {exc}")
                continue
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
                print(f"    ! {len(missing)} roles unjudged in batch {num}")
        return results

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
            print("  categorisation: no archetypes in career-goals.md, skipped")
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

        if not todo:
            print("  categorisation: all uncategorised roles cached")
            return 0

        print(f"  categorising {len(todo)} uncategorised role(s) in 1 call...", flush=True)
        valid_ids = {a["id"] for a in archetypes}
        by_id = {a["id"]: a["label"] for a in archetypes}
        try:
            data = self.llm.complete_json(self._cat_prompt(todo, archetypes),
                                          tag=f"role-cat-{len(todo)}")
        except LLMError as exc:
            print(f"    ! categorisation failed: {exc}")
            return 0

        if isinstance(data, dict):
            data = data.get("roles") or data.get("results") or list(data.values())

        verdicts = {}
        wanted = {r["id"] for r in todo}
        for item in data or []:
            if isinstance(item, dict) and item.get("id") in wanted:
                verdicts[item["id"]] = item

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

    # ---------------- org sector enrichment ----------------

    # ---------------- manual add ----------------

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
        df.to_excel(self.tracker_path, index=False)

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
                data = self.llm.complete_json(prompt, tag="org-sectors")
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
        if not self.tracker_path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.tracker_path.with_name(f"org-roles-tracker-backup-{stamp}.xlsx")
        dest.write_bytes(self.tracker_path.read_bytes())
        print(f"  backup -> {dest.name}")
        self.prune_backups()
        return dest

    def prune_backups(self, keep=KEEP_BACKUPS):
        """Keep the newest `keep` backups written by this agent, delete older ones."""
        mine = sorted(
            (p for p in self.tracker_path.parent.glob("org-roles-tracker-backup-*.xlsx")
             if BACKUP_RE.match(p.name)),
            reverse=True,
        )
        dropped = []
        for path in mine[keep:]:
            try:
                path.unlink()
                dropped.append(path.name)
            except OSError as exc:
                print(f"    ! could not remove {path.name}: {exc}")
        if dropped:
            print(f"  pruned {len(dropped)} old backup(s), kept newest {keep}")
        return dropped

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

            # Suggestion column only. The user's "Role Cat" is never touched.
            if _clean(role.get("role_cat")):
                df.at[idx, SUGGESTED_CAT_COLUMN] = pd.NA   # user decided; drop the proposal
            elif role.get("suggested_role_cat"):
                confidence = role.get("suggestion_confidence") or "?"
                df.at[idx, SUGGESTED_CAT_COLUMN] = f"{role['suggested_role_cat']} ({confidence})"

        # Safety: no row loss, no manual-column mutation.
        assert len(df) == before_shape[0], "row count changed"
        for column in MANUAL_COLUMNS:
            if column in before.columns:
                same = before[column].astype(str).equals(df[column].astype(str))
                assert same, f"manual column mutated: {column}"

        df.to_excel(self.tracker_path, index=False)
        print(f"  tracker {before_shape} -> {df.shape}")
        return df

    # ---------------- run ----------------

    def run(self, seed_fits=None):
        """seed_fits: {role_id: role dict with fit_fingerprint + fit fields} to reuse."""
        print("Running Job Intel...")
        df = self.load_tracker()
        tracked = self.tracker_roles(df)
        print(f"  tracker: {len(tracked)} roles")

        live = self.prefilter_live(self.fetch_live_roles(), {r["id"] for r in tracked})
        roles = tracked + live

        fresh, reused = self.split_by_freshness(roles, seed=seed_fits)
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

        for role in roles:
            for field in CAT_FIELDS:
                role.setdefault(field, None)
        self.categorize_roles(roles, load_archetypes(self.base_dir, df))

        sectors = self.enrich_orgs({r["org"] for r in roles})
        for role in roles:
            role["sector"] = sectors.get(role["org"])

        if not df.empty:
            self.backup_tracker()
            self.update_tracker(df, roles)

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
        print(f"  {self.llm.report()}")
        return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Honest LLM fit analysis for tracked and live roles.")
    ap.add_argument("--refresh", action="store_true",
                    help="re-judge every role, ignoring stored verdicts (costs full price)")
    ap.add_argument("--max-live", type=int, default=45, help="cap on live postings judged")
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

"""
The target brief: what the Profile evaluation measures your public profile against.

It blends what you SAY you want with what you DO, weighted toward what you do:

  pursued roles   roles you researched, applied to or heard back on (the same
                  test as the preferences learner), plus roles the fit judge
                  scored >= pursued_min_fit - unless you closed them for a
                  reason about the role itself. Only the last
                  pursued_window_days count.
  recurring asks  what those roles' descriptions ask for, counted per role
  stated targets  me/profile.md: Target Keywords, Watch Topics
  persona         `## Persona` in me/profile.md, else `## Career Positioning`,
                  else a neutral default - the voice register is judged against
  learned         me/learned.md, the preferences you approved
  claims          what your resume says you have done, for the proof check

Two halves, deliberately split, as in preferences.py: a cheap model call reads
each description and lists its asks as short terms (cached, so a description is
read once), a second maps different wordings of one ask to a canonical term
picked from the list, and Python does ALL the counting. No number in the brief
comes from a model.

    uv run agents/profile_brief.py      # print the brief
"""
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from profile_keywords import load_keywords, load_watch_topics, strip_html_comments  # noqa: E402

TOP_ASKS = 25
MIN_ROLES_WITH_JD = 5
ASK_BATCH = 10
MAX_CLAIMS = 20
PERSONA_HEADINGS = ("## Persona", "## Career Positioning")
NEUTRAL_PERSONA = ("A senior practitioner: specific, evidence-led and credible to a hiring "
                   "manager, without hype or filler.")


# ---------- pursued roles ----------

def pursued_roles(base_dir, config, now=None):
    """Roles that say what you are going for, newest first."""
    from preferences import decision_of
    from store import PREFERENCE_CLOSE_REASONS, Store

    now = now or datetime.now()
    since = (now - timedelta(days=config["pursued_window_days"])).strftime("%Y-%m-%d")
    min_fit = config["pursued_min_fit"]

    with Store(base_dir).connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM roles")]

    out = []
    for row in rows:
        acted = decision_of(row) == "pursued"
        try:
            fit = int(float(row.get("fit_score"))) if row.get("fit_score") is not None else None
        except (TypeError, ValueError):
            fit = None
        rejected = row.get("status") == "04 Closed" and row.get("close_reason") in PREFERENCE_CLOSE_REASONS
        liked = fit is not None and fit >= min_fit and not rejected
        if not (acted or liked):
            continue
        dated = (row.get("date_applied") or row.get("date_opened") or "")[:10]
        if dated and dated < since:
            continue
        if not dated and not acted:
            continue
        out.append({
            "id": row["id"], "org": row.get("org"), "title": row.get("title"),
            "function": row.get("role_function"), "fit_score": fit,
            "why": "acted on" if acted else f"fit {fit}", "date": dated or None,
        })
    out.sort(key=lambda r: r["date"] or "", reverse=True)
    return out


def _jd_cache(base_dir):
    path = Path(base_dir) / "data/job-descriptions.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# ---------- asks ----------

def normalise_ask(term):
    term = str(term or "").lower().replace("-", " ").replace("_", " ")
    term = re.sub(r"[^a-z0-9+#/&. ]+", " ", term)
    return re.sub(r"\s+", " ", term).strip(" .")


def _asks_prompt(batch):
    listing = "\n\n".join(f"[{key}] {role['title']} at {role['org']}\n{jd}" for key, role, jd in batch)
    return f"""Read each job description below and list what it ASKS the candidate for:
skills, methods, tools, domains and kinds of experience that a hiring manager would
screen on.

RULES:
  - 3 to 10 asks per role, the most important first.
  - Each ask is a short lowercase term of 1 to 4 words, in the most common wording
    for it ("experiment design", "causal inference", "sql", "marketing mix modeling",
    "stakeholder management", "people management").
  - Use the same wording for the same thing across roles, so asks can be counted.
  - Leave out boilerplate that every posting has ("communication skills",
    "team player", "fast-paced environment", "bachelor's degree").
  - Only what the description says. Do not infer from the title alone.

ROLES:
{listing}

Return ONLY a JSON object mapping each role key to its list of asks:
{{"r1": ["...", "..."], "r2": ["..."]}}"""


def _asks_validator(keys):
    def validate(data):
        if not isinstance(data, dict) or set(data) != set(keys):
            return False
        return all(isinstance(v, list) and all(isinstance(t, str) for t in v)
                   for v in data.values())
    return validate


def extract_asks(llm, roles, jds):
    """{role_id: [ask, ...]} for every role that has a description."""
    have = [(r, jds[r["id"]]) for r in roles if jds.get(r["id"])]
    out = {}
    for start in range(0, len(have), ASK_BATCH):
        chunk = have[start:start + ASK_BATCH]
        batch = [(f"r{i + 1}", role, jd) for i, (role, jd) in enumerate(chunk)]
        data = llm.complete_json(_asks_prompt(batch), tag="profile-asks",
                                 validate=_asks_validator([k for k, _, _ in batch]))
        for key, role, _ in batch:
            terms = [normalise_ask(t) for t in data.get(key, [])]
            out[role["id"]] = list(dict.fromkeys(t for t in terms if t))
    return out


def _merge_prompt(terms):
    listing = "\n".join(f"- {t}" for t in terms)
    return f"""Below are skill and experience terms pulled from job descriptions. Several
are different wordings of the same thing ("experimentation", "experiment design",
"a/b testing"). Map each such wording to one canonical term, so the same ask can
be counted once.

RULES:
  - The canonical term must be one of the terms in the list - pick the clearest,
    most common wording in the group.
  - Merge only true synonyms or near-synonyms. Keep related-but-different asks
    apart ("causal inference" is not "experiment design"; "sql" is not "python").
  - List ONLY terms that change. A term with no synonym, or that is itself the
    canonical term, is left out.

TERMS:
{listing}

Return ONLY a JSON object from each changed term to its canonical term, or {{}}:
{{"<term>": "<canonical term>", ...}}"""


def _merge_validator(terms):
    allowed = set(terms)

    def validate(data):
        return (isinstance(data, dict) and set(data) <= allowed
                and all(isinstance(v, str) and v in allowed for v in data.values()))
    return validate


def merge_synonyms(llm, asks_by_role, log=print):
    """Fold different wordings of one ask into a canonical term chosen from the
    list itself. The model only maps terms; Python applies the map and counts.
    If the call fails, the asks are counted unmerged - a noisier brief, not none."""
    from llm import LLMError, output_budget

    terms = sorted({t for ts in asks_by_role.values() for t in ts})
    if len(terms) < 2:
        return asks_by_role
    try:
        mapping = llm.complete_json(_merge_prompt(terms), tag="profile-ask-merge",
                                    validate=_merge_validator(terms),
                                    max_tokens=output_budget(len(terms), per_item=20, overhead=3000))
    except LLMError as exc:
        log(f"   ask merge failed, counting unmerged: {exc}")
        return asks_by_role
    # One hop only: a canonical term that is itself mapped elsewhere stays put.
    mapping = {t: c for t, c in mapping.items() if c not in mapping}
    return {rid: list(dict.fromkeys(mapping.get(t, t) for t in ts)) for rid, ts in asks_by_role.items()}


def count_asks(asks_by_role, top=TOP_ASKS):
    """Roles per ask - each ask counted once per role - most asked first."""
    counts = Counter(term for terms in asks_by_role.values() for term in set(terms))
    n = len(asks_by_role)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return [{"ask": term, "roles": c, "share": round(c / n, 2)} for term, c in ranked]


# ---------- the stated side ----------

def section_text(text, heading):
    """The raw text under one `## Heading`, up to the next `## `, comments removed."""
    lines, inside = [], False
    for line in strip_html_comments(text).splitlines():
        if line.strip() == heading or line.strip().startswith(heading + " "):
            inside = True
            continue
        if inside:
            if line.startswith("## "):
                break
            lines.append(line)
    return "\n".join(lines).strip()


def load_persona(base_dir):
    """{"source": heading or "default", "text": ...}"""
    path = Path(base_dir) / "me/profile.md"
    text = path.read_text() if path.exists() else ""
    for heading in PERSONA_HEADINGS:
        body = section_text(text, heading)
        if body:
            return {"source": heading.lstrip("# "), "text": body[:2000]}
    return {"source": "default", "text": NEUTRAL_PERSONA}


def _claims_prompt(resume):
    return f"""Below is a resume. List the concrete claims it makes that a hiring manager
might want to see backed up in public: results, systems built, methods used in
production, scale, leadership. Up to {MAX_CLAIMS}, most significant first.

Each claim is one short sentence in plain words, faithful to the resume - no new
facts, no embellishment. Skip dates, contact details and education lines.

RESUME:
{resume[:12000]}

Return ONLY a JSON array: [{{"claim": "..."}}]"""


def _claims_valid(data):
    return isinstance(data, list) and all(
        isinstance(c, dict) and isinstance(c.get("claim"), str) and c["claim"].strip() for c in data)


def extract_claims(llm, resume):
    if not resume.strip():
        return []
    data = llm.complete_json(_claims_prompt(resume), tag="profile-claims", validate=_claims_valid)
    return [c["claim"].strip() for c in data][:MAX_CLAIMS]


# ---------- the brief ----------

def build_brief(base_dir, config, llm=None, now=None):
    from context import load_context
    from llm import LLM

    base_dir = Path(base_dir)
    llm = llm or LLM(base_dir)
    roles = pursued_roles(base_dir, config, now=now)
    jds = {rid: (entry or {}).get("jd") for rid, entry in _jd_cache(base_dir).items()}
    with_jd = [r for r in roles if jds.get(r["id"])]

    fallback = len(with_jd) < MIN_ROLES_WITH_JD
    stated = load_keywords(base_dir)["target"]
    if fallback:
        asks = [{"ask": normalise_ask(k), "roles": None, "share": None} for k in stated][:TOP_ASKS]
    else:
        asks = count_asks(merge_synonyms(llm, extract_asks(llm, with_jd, jds)))

    learned_path = base_dir / "me/learned.md"
    learned = strip_html_comments(learned_path.read_text()).strip() if learned_path.exists() else ""
    functions = Counter(r["function"] for r in roles if r["function"]).most_common(5)

    brief = {
        "generated": (now or datetime.now()).isoformat(timespec="seconds"),
        "window_days": config["pursued_window_days"],
        "min_fit": config["pursued_min_fit"],
        "pursued": len(roles),
        "with_jd": len(with_jd),
        "fallback": fallback,
        "asks": asks,
        "functions": [{"function": f, "roles": n} for f, n in functions],
        "stated_keywords": stated,
        "watch_topics": [t["topic"] for t in load_watch_topics(base_dir)],
        "persona": load_persona(base_dir),
        "learned": learned[:3000],
        "claims": extract_claims(llm, load_context(base_dir)["resume"]),
    }
    brief["hash"] = brief_hash(brief)
    return brief


def brief_hash(brief):
    body = {k: v for k, v in brief.items() if k not in ("generated", "hash")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def top_asks(brief, n=10):
    return [a["ask"] for a in brief["asks"][:n]]


def render_brief(brief):
    lines = [f"Target brief ({brief['generated'][:10]})",
             f"  pursued roles: {brief['pursued']} in the last {brief['window_days']} days "
             f"(acted on, or fit >= {brief['min_fit']}); {brief['with_jd']} with a description"]
    if brief["fallback"]:
        lines.append(f"  fewer than {MIN_ROLES_WITH_JD} descriptions - using your Target Keywords instead")
    lines.append("  recurring asks:")
    for a in brief["asks"]:
        count = f"{a['roles']:>3} of {brief['with_jd']}" if a["roles"] is not None else "stated"
        lines.append(f"    {count}  {a['ask']}")
    if brief["functions"]:
        lines.append("  functions: " + ", ".join(f"{f['function']} ({f['roles']})" for f in brief["functions"]))
    lines.append(f"  persona from: {brief['persona']['source']}")
    lines.append(f"  resume claims: {len(brief['claims'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    import workspace
    from profile_snapshot import load_config

    base = workspace.resolve()
    workspace.load_env(base)
    print(render_brief(build_brief(base, load_config(base))))

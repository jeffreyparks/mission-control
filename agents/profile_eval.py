"""
Profile evaluation: is your public profile on target for the roles you pursue?

Judges the latest snapshot of every source against the target brief
(profile_brief.py), on six dimensions:

  coverage     do the recurring asks appear anywhere public?
  drift        how much of your public voice goes to off-target topics?
  consistency  do headline, bios and taglines tell one story?
  proof        are resume claims backed by something visible?
  register     does the voice match your persona?
  freshness    does anything look abandoned?  (a pure rule - no model)

Python computes every count and share; a model only scores and words things.
  1. Python: which sources mention each ask; freshness by rule.
  2. One call per source (tag profile-eval-source): scores, an on-target /
     adjacent / tangent tag per item, candidate findings.
  3. Python: drift shares from the tags (reposts weigh half); every quote is
     checked against the snapshot text, and a finding whose quote is not there
     is dropped and logged; role counts are attached to asks.
  4. One synthesis call (tag profile-eval-synth): overall scores, a short
     verdict, cross-source consistency findings, one ranked list.

Model answers are cached by prompt, so an unchanged source costs nothing to
re-judge. An evaluation runs when a source changed, when reevaluate_days have
passed, when the brief's top asks changed, or when forced.
"""
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DIMENSIONS = ("coverage", "drift", "consistency", "proof", "register", "freshness")
SOURCE_DIMENSIONS = ("coverage", "drift", "proof", "register")
SEVERITIES = ("high", "medium", "low")
TAGS = ("on", "adjacent", "tangent")
ITEM_CHARS = 1500
SOURCE_CHARS = 40000
QUOTE_CHARS = 200


# ---------- text helpers ----------

def _norm(text):
    text = (text or "").lower()
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                         "–": "-", "—": "-", " ": " "}))
    return re.sub(r"\s+", " ", text).strip()


def quote_in(quote, text_norm):
    """Is the quote really in the (normalised) text? A leading field label the
    model copied from the prompt ("company: Initech") is allowed and ignored."""
    q = _norm(quote)
    if q and q in text_norm:
        return True
    label = re.match(r"^[a-z_ ]{2,20}:\s*(.+)$", q)
    return bool(label) and label.group(1) in text_norm


def source_text(snap):
    from profile_snapshot import snapshot_text
    return snapshot_text(snap)


def mentions(text_norm, ask):
    """Does normalised text mention an ask? Spaces and hyphens are interchangeable."""
    ask = _norm(ask)
    return bool(ask) and (ask in text_norm or ask.replace(" ", "-") in text_norm)


def coverage_matrix(brief, snaps):
    """{ask: {source_key: bool}} - exact mentions, in Python."""
    texts = {s["source_key"]: _norm(source_text(s)) for s in snaps}
    return {a["ask"]: {key: mentions(text, a["ask"]) for key, text in texts.items()}
            for a in brief["asks"]}


def freshness_score(snap):
    """5 active this month, 4 this quarter, 2 over 90 days, 1 over 180; None = no dates."""
    days = snap.get("stats", {}).get("days_since_activity")
    if days is None and snap.get("stats", {}).get("last_activity"):
        try:
            days = (datetime.now() - datetime.strptime(snap["stats"]["last_activity"][:10],
                                                       "%Y-%m-%d")).days
        except ValueError:
            days = None
    if days is None:
        return None
    return 5 if days <= 30 else 4 if days <= 90 else 2 if days <= 180 else 1


def drift_shares(snap, tags):
    """Share of the source's (weighted) items tagged on / adjacent / tangent."""
    totals = dict.fromkeys(TAGS, 0.0)
    for item in snap["items"]:
        tag = tags.get(item["id"])
        if tag in totals:
            totals[tag] += item.get("weight", 1.0)
    whole = sum(totals.values())
    return {t: round(v / whole, 2) for t, v in totals.items()} if whole else None


def fingerprint(finding):
    """Stable across rewordings: what, where, and about which ask (or which words)."""
    anchor = finding.get("ask") or _norm(finding.get("quote") or "")[:80] or _norm(finding["title"])
    raw = f"{finding['dimension']}|{finding.get('source_key') or '*'}|{anchor}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ---------- per-source call ----------

def _today_line(now=None):
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    return (f"Today is {today}. Dates on or before today are in the past or present - "
            f"never flag them as future or impossible.")


def _guidance(text):
    """The approved review guidance, set off by blank lines; nothing when there is none."""
    return f"\n{text}" if text else ""


def _brief_block(brief):
    asks = "\n".join(
        f"  - {a['ask']}" + (f"  ({a['roles']} of {brief['with_jd']} pursued roles)" if a["roles"] else "")
        for a in brief["asks"])
    claims = "\n".join(f"  - {c}" for c in brief["claims"]) or "  (none)"
    return f"""TARGET BRIEF
Roles the candidate is pursuing: {', '.join(f['function'] for f in brief['functions']) or 'see asks'}
What those roles ask for, most asked first:
{asks}
Stated target keywords: {', '.join(brief['stated_keywords'][:40]) or '(none)'}
Watch topics (emerging themes they want to be known for): {', '.join(brief['watch_topics']) or '(none)'}
Intended persona ({brief['persona']['source']}):
{brief['persona']['text']}
Resume claims:
{claims}"""


def _items_block(snap):
    keyed, lines, used = {}, [], 0
    for n, item in enumerate(snap["items"], 1):
        if not (item.get("title") or item.get("text")):
            continue
        key = f"i{n}"
        keyed[key] = item
        head = f"[{key}] {item['kind']}" + (f" ({item['date']})" if item.get("date") else "")
        if item.get("weight", 1.0) != 1.0:
            head += f" weight {item['weight']}"
        body = f"{item.get('title') or ''}\n{(item.get('text') or '')[:ITEM_CHARS]}".strip()
        if used + len(body) > SOURCE_CHARS:
            lines.append("[... remaining items left out for length]")
            break
        used += len(body)
        lines.append(f"{head}\n{body}")
    return keyed, "\n\n".join(lines)


def _source_prompt(brief, snap, coverage, items_text, now=None, guidance=""):
    identity = "\n".join(f"  {k}: {v}" for k, v in snap["identity"].items())
    covered = [a for a, by in coverage.items() if by.get(snap["source_key"])]
    missing = [a for a, by in coverage.items() if not by.get(snap["source_key"])]
    return f"""You review a job candidate's public profile the way a hiring manager for the
roles below would read it. Judge THIS source only: {snap['source']} ({snap['source_key']}).
{_today_line(now)}

{_brief_block(brief)}
{_guidance(guidance)}
EXACT-MATCH COVERAGE ON THIS SOURCE (computed, may miss synonyms):
  mentioned: {', '.join(covered) or '(none)'}
  not found: {', '.join(missing) or '(none)'}

IDENTITY (what a visitor reads first):
{identity or '  (empty)'}

ITEMS:
{items_text or '(no items)'}

Score each dimension 1 (poor) to 5 (strong) for this source:
  coverage  - how well it shows what the target roles ask for (count synonyms and
              clear demonstrations, not just exact words)
  drift     - 5 = almost everything is on target; 1 = mostly other topics
  proof     - how much of the resume's claims it backs with something visible
  register  - how well the voice matches the intended persona

Tag EVERY item key listed above: "on" (serves the target roles), "adjacent"
(related professional ground), or "tangent" (unrelated to the target roles).

Then list findings: specific, fixable problems a hiring manager would notice.
  - "quote": the exact words from the identity or an item, copied character for
    character, at most {QUOTE_CHARS} characters; or null for an ABSENCE finding
    ("never mentions X"), which must name the ask it is about.
  - "ask": the brief ask it relates to, exactly as written above, or null.
  - "fix": one concrete action on this source.
  - At most 8 findings, most important first. No generic advice ("post more",
    "add a banner") unless it is tied to an ask or the persona.

Return ONLY JSON:
{{"scores": {{"coverage": 1-5, "drift": 1-5, "proof": 1-5, "register": 1-5}},
  "score_notes": {{"coverage": "<clause>", "drift": "<clause>", "proof": "<clause>", "register": "<clause>"}},
  "item_tags": {{"i1": "on" | "adjacent" | "tangent", ...}},
  "findings": [{{"dimension": "coverage" | "drift" | "proof" | "register",
                "severity": "high" | "medium" | "low", "title": "<one line>",
                "quote": "<exact words>" | null, "item": "<item key>" | null,
                "ask": "<ask>" | null, "fix": "<one action>"}}]}}"""


def _valid_finding(f, dims):
    return (isinstance(f, dict) and f.get("dimension") in dims and f.get("severity") in SEVERITIES
            and isinstance(f.get("title"), str) and f["title"].strip()
            and isinstance(f.get("fix"), str) and f["fix"].strip()
            and (f.get("quote") is None or isinstance(f.get("quote"), str)))


def _source_validator(data):
    if not isinstance(data, dict):
        return False
    scores = data.get("scores")
    if not isinstance(scores, dict) or any(
            not isinstance(scores.get(d), int) or not 1 <= scores[d] <= 5 for d in SOURCE_DIMENSIONS):
        return False
    if not isinstance(data.get("item_tags"), dict) or not isinstance(data.get("findings"), list):
        return False
    return all(_valid_finding(f, SOURCE_DIMENSIONS) for f in data["findings"])


def _check_findings(raw, snap, keyed, asks, coverage, log):
    """Keep findings whose quote is really in the snapshot; attach urls and role counts."""
    text = _norm(source_text(snap))
    kept = []
    for f in raw:
        ask = _norm(f.get("ask")) if f.get("ask") else None
        ask = ask if ask in asks else None
        quote = (f.get("quote") or "").strip()[:QUOTE_CHARS] or None
        if quote:
            if not quote_in(quote, text):
                log(f"   dropped ({snap['source_key']}): quote not found - {quote[:60]!r}")
                continue
        else:
            if not ask:
                log(f"   dropped ({snap['source_key']}): absence finding names no ask - {f['title'][:60]!r}")
                continue
            if coverage.get(ask, {}).get(snap["source_key"]):
                log(f"   dropped ({snap['source_key']}): says {ask!r} is absent, but it is there")
                continue
        item = keyed.get(f.get("item") or "")
        kept.append({
            "dimension": f["dimension"], "severity": f["severity"], "title": f["title"].strip(),
            "quote": quote, "url": (item or {}).get("url") or _profile_url(snap),
            "ask": ask, "ask_roles": asks.get(ask) if ask else None,
            "fix": f["fix"].strip(), "source_key": snap["source_key"],
        })
    return kept


def _profile_url(snap):
    key = snap["source_key"]
    if key.startswith("github:"):
        return f"https://github.com/{key.split(':', 1)[1]}"
    if key.startswith("bluesky:"):
        return f"https://bsky.app/profile/{key.split(':', 1)[1]}"
    if key.startswith("site:"):
        return key.split(":", 1)[1]
    return None


def judge_source(llm, brief, snap, coverage, log=print, now=None, guidance=""):
    keyed, items_text = _items_block(snap)
    data = llm.complete_json(_source_prompt(brief, snap, coverage, items_text, now=now,
                                            guidance=guidance),
                             tag="profile-eval-source", validate=_source_validator, max_tokens=8000)
    tags = {keyed[k]["id"]: v for k, v in data["item_tags"].items() if k in keyed and v in TAGS}
    asks = {a["ask"]: a["roles"] for a in brief["asks"]}
    return {
        "source_key": snap["source_key"],
        "snapshot_id": snap.get("_id"),
        "scores": {d: data["scores"][d] for d in SOURCE_DIMENSIONS},
        "score_notes": data.get("score_notes") or {},
        "freshness": freshness_score(snap),
        "shares": drift_shares(snap, tags),
        "tagged": len(tags),
        "findings": _check_findings(data["findings"], snap, keyed, asks, coverage, log),
        "stale": bool(snap.get("stats", {}).get("stale")),
    }


# ---------- synthesis ----------

def _synth_prompt(brief, snaps, results, now=None, guidance=""):
    identities = "\n\n".join(
        f"[{s['source_key']}]\n" + "\n".join(f"  {k}: {str(v)[:600]}" for k, v in s["identity"].items())
        for s in snaps)
    findings, n = [], 0
    for r in results:
        for f in r["findings"]:
            n += 1
            f["_ref"] = f"f{n}"
            findings.append(f"[f{n}] {r['source_key']} | {f['dimension']} | {f['severity']} | {f['title']}"
                            + (f" | quote: {f['quote']!r}" if f["quote"] else ""))
    per_source = "\n".join(
        f"  {r['source_key']}: scores {r['scores']}, freshness {r['freshness']}, drift shares {r['shares']}"
        + (" (export is stale)" if r["stale"] else "") for r in results)
    return f"""You are completing a review of a job candidate's public profile across all
their sources, for the roles below.
{_today_line(now)}

{_brief_block(brief)}
{_guidance(guidance)}
PER-SOURCE RESULTS (scores 1-5; shares are computed):
{per_source}

IDENTITY TEXT BY SOURCE (headline, bio, tagline - what a visitor reads first):
{identities}

CANDIDATE FINDINGS FROM THE PER-SOURCE REVIEWS:
{chr(10).join(findings) or '(none)'}

Do four things:
  1. Score the whole public profile 1-5 on coverage, drift, consistency, proof,
     register. Consistency is new here: do the identity texts tell one story about
     role, level and focus?
  2. Write a 2-3 sentence verdict for the candidate: is the profile on target, and
     what is the single most important thing to change?
  3. Rank the candidate findings by importance, keeping each distinct problem once:
     list the refs to keep, most important first; leave out duplicates, trivia,
     and any the review guidance rules out.
  4. Add consistency findings (at most 4): places where sources disagree on role,
     level or focus. Each quotes the exact identity words it is about.

Return ONLY JSON:
{{"overall": {{"coverage": 1-5, "drift": 1-5, "consistency": 1-5, "proof": 1-5, "register": 1-5}},
  "summary": "<2-3 sentences>",
  "keep": ["f3", "f1", ...],
  "consistency_findings": [{{"source_key": "<source>", "severity": "high" | "medium" | "low",
      "title": "<one line>", "quote": "<exact identity words>", "fix": "<one action>"}}]}}"""


def _synth_validator(refs):
    def validate(data):
        if not isinstance(data, dict):
            return False
        overall = data.get("overall")
        if not isinstance(overall, dict) or any(
                not isinstance(overall.get(d), int) or not 1 <= overall[d] <= 5 for d in DIMENSIONS[:5]):
            return False
        if not isinstance(data.get("summary"), str) or not data["summary"].strip():
            return False
        if not isinstance(data.get("keep"), list) or any(k not in refs for k in data["keep"]):
            return False
        extra = data.get("consistency_findings", [])
        return isinstance(extra, list) and all(
            _valid_finding({**f, "dimension": "consistency"}, ("consistency",)) for f in extra)
    return validate


def synthesise(llm, brief, snaps, results, log=print, now=None, guidance=""):
    prompt = _synth_prompt(brief, snaps, results, now=now, guidance=guidance)
    by_ref = {f["_ref"]: f for r in results for f in r["findings"]}
    data = llm.complete_json(prompt, tag="profile-eval-synth",
                             validate=_synth_validator(set(by_ref)), max_tokens=6000)

    ranked = [by_ref[k] for k in dict.fromkeys(data["keep"])]
    identity_by_key = {s["source_key"]: _norm(" ".join(str(v) for v in s["identity"].values()))
                       for s in snaps}
    all_identity = " ".join(identity_by_key.values())
    for f in data.get("consistency_findings", []):
        quote = (f.get("quote") or "").strip()[:QUOTE_CHARS]
        if not quote or not quote_in(quote, all_identity):
            log(f"   dropped (consistency): quote not in any identity text - {quote[:60]!r}")
            continue
        key = f.get("source_key") if f.get("source_key") in identity_by_key else None
        if key is None or not quote_in(quote, identity_by_key[key]):
            key = next((k for k, t in identity_by_key.items() if quote_in(quote, t)), None)
        ranked.append({"dimension": "consistency", "severity": f["severity"], "title": f["title"].strip(),
                       "quote": quote, "url": None, "ask": None, "ask_roles": None,
                       "fix": f["fix"].strip(), "source_key": key})

    for n, f in enumerate(ranked):
        f.pop("_ref", None)
        f["rank"] = n + 1
        f["fingerprint"] = fingerprint(f)
    for r in results:
        for f in r["findings"]:
            f.pop("_ref", None)

    freshness = [r["freshness"] for r in results if r["freshness"] is not None]
    overall = {d: data["overall"][d] for d in DIMENSIONS[:5]}
    overall["freshness"] = min(freshness) if freshness else None
    unique = {f["fingerprint"]: f for f in ranked}
    return {"overall": overall, "summary": data["summary"].strip(), "findings": list(unique.values())}


# ---------- orchestration ----------

def should_evaluate(store, brief, snaps, config, force=False, now=None):
    """(yes, why)."""
    from profile_brief import top_asks

    if force:
        return True, "forced"
    last = store.latest_evaluation()
    if last is None:
        return True, "first evaluation"
    current = {s["source_key"]: s["_id"] for s in snaps}
    before = last["snapshot_ids"] or {}
    changed = sorted(k for k in current if before.get(k) != current[k])
    if changed:
        return True, "changed: " + ", ".join(changed)
    if top_asks(brief) != top_asks(last["brief"]):
        return True, "the brief's top asks changed"
    now = now or datetime.now()
    age = (now - datetime.fromisoformat(last["created_at"])).days
    if age >= config["reevaluate_days"]:
        return True, f"{age} days since the last evaluation"
    return False, f"nothing changed; next check in {config['reevaluate_days'] - age} days"


def evaluate(base_dir, force=False, llm=None, log=print, now=None):
    """Build the brief, decide, judge, store. Returns (evaluation_id or None, why)."""
    from context import guidance_block
    from llm import LLM
    from profile_brief import build_brief
    from profile_snapshot import load_config
    from store import Store

    base_dir = Path(base_dir)
    config = load_config(base_dir)
    store = Store(base_dir)
    snaps = store.latest_snapshots()
    if not snaps:
        return None, "no profile snapshots yet - run mc profile --collect-only first"

    llm = llm or LLM(base_dir)
    brief = build_brief(base_dir, config, llm=llm, now=now)
    due, why = should_evaluate(store, brief, snaps, config, force=force, now=now)
    if not due:
        return None, why

    log(f"   evaluating ({why}): {len(snaps)} sources, {len(brief['asks'])} asks "
        f"from {brief['with_jd']} role descriptions")
    coverage = coverage_matrix(brief, snaps)
    guidance = guidance_block(base_dir)
    results = []
    for snap in snaps:
        try:
            results.append(judge_source(llm, brief, snap, coverage, log=log, now=now,
                                        guidance=guidance))
        except Exception as exc:  # noqa: BLE001 - one source must not sink the evaluation
            log(f"   {snap['source_key']}: evaluation failed - {exc}")
    if not results:
        return None, "every source failed to evaluate"
    judged = [s for s in snaps if s["source_key"] in {r["source_key"] for r in results}]
    synth = synthesise(llm, brief, judged, results, log=log, now=now, guidance=guidance)

    stats = llm.stats
    record = {
        "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "trigger": why,
        "snapshot_ids": {s["source_key"]: s["_id"] for s in judged},
        "brief_hash": brief["hash"],
        "brief": brief,
        "scores": {"overall": synth["overall"],
                   "per_source": {r["source_key"]: {**r["scores"], "freshness": r["freshness"]}
                                  for r in results}},
        "shares": {r["source_key"]: r["shares"] for r in results},
        "per_source": results,
        "summary": synth["summary"],
        "model_calls": stats.get("misses"),
        "tokens_in": stats.get("api_input_tokens") or None,
        "tokens_out": stats.get("api_output_tokens") or None,
    }
    from profile_drafts import draft_for_latest, unpublished_findings
    findings = synth["findings"] + unpublished_findings(base_dir, judged)
    evaluation_id = store.add_evaluation(record)
    counts = store.sync_findings(evaluation_id, findings, now=record["created_at"])
    log(f"   evaluation {evaluation_id}: {len(findings)} findings "
        f"({counts['new']} new, {counts['kept']} standing, {counts['fixed']} fixed, "
        f"{counts['reopened']} reopened, {counts['held']} held - profile unchanged)")
    try:
        draft_for_latest(base_dir, llm=llm, log=log, now=now)
    except Exception as exc:  # noqa: BLE001 - drafts must not sink a stored evaluation
        log(f"   drafts failed: {exc}")
    log(f"   {llm.report()}")
    return evaluation_id, why


# ---------- report ----------

def render_report(store):
    """Markdown copy of the latest evaluation and the standing findings."""
    ev = store.latest_evaluation()
    if ev is None:
        return None
    brief = ev["brief"]
    overall = ev["scores"]["overall"]
    lines = [f"# Profile evaluation - {ev['created_at'][:10]}", "",
             f"Trigger: {ev['trigger']}. Judged against {brief['pursued']} pursued roles "
             f"({brief['with_jd']} with descriptions) from the last {brief['window_days']} days.", "",
             ev["summary"] or "", "", "## Scores", "",
             "| Dimension | Overall | " + " | ".join(ev["scores"]["per_source"]) + " |",
             "| --- | --- | " + " | ".join("---" for _ in ev["scores"]["per_source"]) + " |"]
    for d in DIMENSIONS:
        cells = [str(v.get(d) if v.get(d) is not None else "-") for v in ev["scores"]["per_source"].values()]
        value = overall.get(d)
        lines.append(f"| {d} | {value if value is not None else '-'} | " + " | ".join(cells) + " |")
    lines += ["", "## Drift", ""]
    for key, shares in (ev["shares"] or {}).items():
        if shares:
            lines.append(f"- {key}: {shares['on']:.0%} on target, {shares['adjacent']:.0%} adjacent, "
                         f"{shares['tangent']:.0%} tangent")
    lines += ["", "## Top asks", ""]
    for a in brief["asks"][:10]:
        lines.append(f"- {a['ask']}" + (f" - {a['roles']} of {brief['with_jd']} roles" if a["roles"] else ""))
    lines += ["", "## Findings", ""]
    for f in store.profile_findings():
        head = f"- **[{f['severity']}] {f['title']}** ({f['dimension']}, {f['source_key'] or 'all sources'})"
        if f["came_back"]:
            head += " - came back"
        lines.append(head)
        if f["quote"]:
            lines.append(f"  > {f['quote']}")
        if f["ask"]:
            lines.append(f"  Ask: {f['ask']}" + (f" ({f['ask_roles']} roles)" if f["ask_roles"] else ""))
        lines.append(f"  Fix: {f['fix']}")
    return "\n".join(lines).rstrip() + "\n"


def write_report(base_dir):
    from store import Store

    text = render_report(Store(base_dir))
    if text is None:
        return None
    out = Path(base_dir) / f"artifacts/profiles/profile-{datetime.now():%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return out


# ---------- Content Radar ----------

GAP_TOPICS_MAX = 5
_SOURCE_NAMES = {"linkedin": "LinkedIn", "github": "GitHub", "bluesky": "BlueSky"}


def _source_name(key):
    kind, _, rest = (key or "").partition(":")
    if kind == "site":
        from urllib.parse import urlparse
        return "your site " + (urlparse(rest).netloc or rest)
    return _SOURCE_NAMES.get(kind, key or "your profiles")


def profile_gap_topics(base_dir, limit=GAP_TOPICS_MAX):
    """Coverage gaps worth writing your way out of, for the Content Radar.

    Open or accepted coverage findings of high or medium severity that name an
    ask - one topic per ask, most severe and most asked-for first. Dismissing
    or fixing the finding removes the topic on the next radar run. Returns
    [{"topic", "note"}], the same shape as load_watch_topics; [] when there is
    no evaluation (or no database) yet.
    """
    from store import DB_NAME
    if not (Path(base_dir) / DB_NAME).exists():
        return []
    try:
        from store import Store
        store = Store(base_dir)
        ev = store.latest_evaluation()
        findings = store.profile_findings() if ev else []
    except Exception:  # noqa: BLE001 - the radar must run without profile data
        return []
    with_jd = (ev or {}).get("brief", {}).get("with_jd")
    rank = {"high": 0, "medium": 1}
    gaps = {}
    for f in findings:
        if f["dimension"] != "coverage" or f["severity"] not in rank or not f["ask"]:
            continue
        g = gaps.setdefault(f["ask"], {"ask": f["ask"], "rank": rank[f["severity"]],
                                       "roles": f["ask_roles"], "sources": []})
        g["rank"] = min(g["rank"], rank[f["severity"]])
        name = _source_name(f["source_key"])
        if name not in g["sources"]:
            g["sources"].append(name)
    ordered = sorted(gaps.values(), key=lambda g: (g["rank"], -(g["roles"] or 0), g["ask"]))[:limit]
    out = []
    for g in ordered:
        asked = (f"{g['roles']} of {with_jd} pursued roles ask for it" if g["roles"] and with_jd
                 else "one of your stated targets")
        out.append({"topic": g["ask"], "note": f"{asked}; not shown on {', '.join(g['sources'])}"})
    return out

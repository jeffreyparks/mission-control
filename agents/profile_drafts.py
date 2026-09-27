"""
Draft rewrites: replacement text for the profile fields a recruiter reads first.

One call (tag profile-drafts) reads the target brief, your persona, the current
text of each field and the open findings, and drafts options per field. Python
then checks every option:

  length   against the platform's limit; over-length options get one retry
           asking to shorten them, and are flagged if still too long
  claims   any number or proper name that appears in neither your resume nor
           your current profiles marks the option "needs check", naming the
           words, because a draft must never claim what you have not

Nothing is applied anywhere. Drafts go to me/persona.draft.md and to the Profile
page; approved text goes to me/persona.md, which is yours - you paste it into
each platform yourself. The next evaluation checks that each approved field
actually appears on the live profile.

    me/persona.draft.md   option 1 live under each heading, alternatives in comments
    me/persona.md         what you approved, one `## <field key>` section per field
"""
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from profile_keywords import strip_html_comments  # noqa: E402

DRAFT = "me/persona.draft.md"
PERSONA = "me/persona.md"
PREVIOUS = "me/persona.prev.md"

# source kind -> [(field, label, limit, options, identity keys to read the current text from)]
FIELD_SPECS = {
    "linkedin": [("headline", "LinkedIn headline", 220, 2, ("headline",)),
                 ("about", "LinkedIn About", 2600, 1, ("about",))],
    "github": [("bio", "GitHub bio", 160, 2, ("bio",)),
               ("readme", "GitHub profile README intro", 600, 1, ("readme",))],
    "bluesky": [("bio", "BlueSky bio", 256, 2, ("bio",))],
    "site": [("tagline", "Site tagline", 120, 2, ("description", "hero"))],
}
FINDINGS_IN_PROMPT = 15


def field_key(source_key, field):
    return f"{source_key}#{field}"


def fields_for(snaps):
    """One entry per draftable field of the sources you have."""
    out = []
    for snap in snaps:
        kind = snap["source_key"].split(":", 1)[0]
        for field, label, limit, options, keys in FIELD_SPECS.get(kind, []):
            current = next((snap["identity"].get(k) for k in keys if snap["identity"].get(k)), "") or ""
            if kind == "site":
                label = f"{label} ({snap['source_key'].split(':', 1)[1]})"
            out.append({"key": field_key(snap["source_key"], field), "source_key": snap["source_key"],
                        "field": field, "label": label, "limit": limit, "options": options,
                        "current": current[:limit * 2]})
    return out


def limits(snaps):
    return {f["key"]: f["limit"] for f in fields_for(snaps)}


# ---------- the claim check ----------

_NUMBER = re.compile(r"\d[\d,.]*")
_NAME = re.compile(r"\b[A-Z][A-Za-z&'\-.]{2,}\b|\b[A-Z]{2,}\b")
_COMMON = {"the", "and", "for", "with", "from", "into", "that", "this", "your", "our", "who", "how",
           "i", "we", "my", "at", "in", "on", "of", "to", "a", "an", "now", "then", "building",
           "leading", "led", "lead", "helping", "turning", "making", "head", "director", "senior",
           "sr", "vp", "data", "science", "marketing", "measurement", "applied", "ai", "ml",
           "prev", "previously", "ex", "former", "formerly", "inc", "llc", "co", "corp", "ltd"}


def _sentence_start(text, at):
    before = text[:at].rstrip()
    return not before or before[-1] in ".!?:|\n-\u2014"


def unsupported(text, reference):
    """Numbers and proper names in `text` that `reference` never mentions.

    Deliberately forgiving about form, strict about substance: a possessive
    ("Inc.'s"), a hyphenated prefix ("Ex-Globex") or a capital at the start of
    a sentence ("Today") is not a new claim; a name or number found nowhere in
    the resume or the profiles is."""
    ref = reference.lower()
    ref_digits = set(re.sub(r"[,.]", "", n) for n in _NUMBER.findall(reference))
    missing = []
    for n in _NUMBER.findall(text):
        core = re.sub(r"[,.]", "", n).rstrip()
        if core and core not in ref_digits and n.strip(".,") not in ref:
            missing.append(n.strip(".,"))
    for m in _NAME.finditer(text):
        word = re.sub(r"['\u2019]s$", "", m.group(0)).strip(".-'\u2019")
        parts = [w for w in re.split(r"[-.]", word) if w]
        if not parts or all(w.lower() in _COMMON or w.lower() in ref for w in parts):
            continue
        if len(parts) == 1 and _sentence_start(text, m.start()):
            # A capital that only marks a sentence start is not a name - unless
            # it looks like one (all caps, or capitalised again mid-sentence).
            flat = re.sub(r"\s+", " ", text)
            if not word.isupper() and not re.search(r"[^.!?:|\s\-\u2014] " + re.escape(word) + r"\b", flat):
                continue
        missing.append(word)
    return list(dict.fromkeys(missing))


# ---------- the call ----------

def _prompt(brief, fields, findings, resume, now=None):
    from profile_eval import _brief_block, _today_line

    field_lines = "\n\n".join(
        f"[{f['key']}] {f['label']} - at most {f['limit']} characters, {f['options']} option(s)\n"
        f"  current: {f['current'][:800] or '(empty)'}" for f in fields)
    finding_lines = "\n".join(f"  [F{x['id']}] ({x['severity']}) {x['title']} - fix: {x['fix']}"
                              for x in findings[:FINDINGS_IN_PROMPT]) or "  (none)"
    return f"""You draft replacement text for a job candidate's public profile fields, so they
read well to a hiring manager for the roles below.
{_today_line(now)}

{_brief_block(brief)}

RESUME (the only source of facts you may use):
{resume[:6000]}

OPEN FINDINGS FROM THE PROFILE REVIEW:
{finding_lines}

FIELDS TO DRAFT:
{field_lines}

RULES:
  - Use ONLY facts in the resume or the current text. Never invent a number, an
    employer, a client, a title or a result. If a fact is not there, leave it out.
  - Write in the intended persona's voice. No hype words, no emoji, no hashtags.
  - Stay within each field's character limit - count carefully.
  - Where options are asked for, make them genuinely different (e.g. one leads with
    the role, one with the outcome).
  - Lead with what the target roles ask for most, where the resume supports it.
  - "addresses": the refs of the findings an option fixes, e.g. ["F12"].

Return ONLY JSON:
{{"drafts": [{{"field": "<field key>", "options": [{{"text": "...", "addresses": ["F12"]}}]}}]}}"""


def _validator(fields):
    want = {f["key"]: f["options"] for f in fields}

    def validate(data):
        if not isinstance(data, dict) or not isinstance(data.get("drafts"), list):
            return False
        seen = {}
        for d in data["drafts"]:
            if not isinstance(d, dict) or d.get("field") not in want or not isinstance(d.get("options"), list):
                return False
            if not d["options"] or not all(isinstance(o, dict) and isinstance(o.get("text"), str)
                                           and o["text"].strip() for o in d["options"]):
                return False
            seen[d["field"]] = d
        return set(seen) == set(want)
    return validate


def _shorten_prompt(over):
    lines = "\n\n".join(f"[{k}] limit {limit} characters, now {len(text)}:\n{text}"
                        for k, (text, limit) in over.items())
    return f"""Shorten each text below to fit its character limit. Keep the meaning, the
facts and the voice; cut words, never add facts.

{lines}

Return ONLY JSON mapping each key to its shortened text: {{"<key>": "..."}}"""


def _shorten_validator(keys):
    def validate(data):
        return (isinstance(data, dict) and set(data) == set(keys)
                and all(isinstance(v, str) and v.strip() for v in data.values()))
    return validate


def make_drafts(llm, brief, snaps, findings, resume, log=print, now=None):
    """[{key, source_key, field, label, limit, current, options: [{text, chars,
    over, needs_check, addresses}]}]"""
    fields = fields_for(snaps)
    if not fields:
        return []
    data = llm.complete_json(_prompt(brief, fields, findings, resume, now=now), tag="profile-drafts",
                             validate=_validator(fields), max_tokens=8000)
    by_key = {d["field"]: d for d in data["drafts"]}
    known = {f"F{x['id']}": x["id"] for x in findings}

    options = {}
    for f in fields:
        for n, o in enumerate(by_key[f["key"]]["options"][:f["options"]]):
            options[(f["key"], n)] = {"text": o["text"].strip(),
                                      "addresses": [known[a] for a in o.get("addresses") or [] if a in known]}

    over = {f"{k}|{n}": (o["text"], f["limit"]) for f in fields for (k, n), o in options.items()
            if k == f["key"] and len(o["text"]) > f["limit"]}
    if over:
        log(f"   drafts: {len(over)} option(s) over the limit - asking once to shorten")
        try:
            short = llm.complete_json(_shorten_prompt(over), tag="profile-drafts",
                                      validate=_shorten_validator(list(over)))
            for ref, text in short.items():
                k, n = ref.rsplit("|", 1)
                options[(k, int(n))]["text"] = text.strip()
        except Exception as exc:  # noqa: BLE001 - flagged below instead
            log(f"   drafts: shortening failed, flagging instead - {exc}")

    from profile_snapshot import snapshot_text
    reference = resume + "\n" + "\n".join(snapshot_text(s) for s in snaps)
    out = []
    for f in fields:
        opts = []
        for (k, n), o in sorted(options.items()):
            if k != f["key"]:
                continue
            opts.append({**o, "chars": len(o["text"]), "over": len(o["text"]) > f["limit"],
                         "needs_check": unsupported(o["text"], reference)})
        out.append({**{k: f[k] for k in ("key", "source_key", "field", "label", "limit", "current")},
                    "options": opts})
    return out


# ---------- files ----------

def render_draft_file(drafts, evaluation_id, now=None):
    now = now or datetime.now()
    lines = ["# Profile drafts", "",
             f"<!-- DRAFT generated {now:%Y-%m-%d} from evaluation {evaluation_id}. Nothing here is applied",
             "     anywhere. Under each heading, the live text is what `mc profile --approve` adopts",
             "     into me/persona.md; the other options are in comments - swap one in, or edit",
             "     freely. You paste approved text into each platform yourself. You can also",
             "     approve field by field on the Profile page. -->", ""]
    for d in drafts:
        if not d["options"]:
            continue
        first, rest = d["options"][0], d["options"][1:]
        lines += [f"## {d['key']}", f"<!-- {d['label']} - limit {d['limit']} characters -->"]
        lines += [first["text"], f"<!-- {first['chars']} characters"
                  + (" - OVER THE LIMIT" if first["over"] else "")
                  + (f" - needs check: {', '.join(first['needs_check'])}" if first["needs_check"] else "")
                  + " -->"]
        for n, o in enumerate(rest, 2):
            lines += [f"<!-- option {n} ({o['chars']} characters"
                      + (f", needs check: {', '.join(o['needs_check'])}" if o["needs_check"] else "")
                      + "):", o["text"].replace("-->", "- ->"), "-->"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_persona(text):
    """{field key: text} from a persona file; comments ignored."""
    out, key, buf = {}, None, []
    for line in strip_html_comments(text or "").splitlines():
        if line.startswith("## "):
            if key:
                out[key] = "\n".join(buf).strip()
            key, buf = line[3:].strip(), []
        elif key:
            buf.append(line)
    if key:
        out[key] = "\n".join(buf).strip()
    return {k: v for k, v in out.items() if v}


def load_approved(base_dir):
    path = Path(base_dir) / PERSONA
    return parse_persona(path.read_text()) if path.exists() else {}


def _write_persona(base_dir, fields):
    path = Path(base_dir) / PERSONA
    if path.exists():
        shutil.copy2(path, Path(base_dir) / PREVIOUS)
    lines = ["# Approved profile text", "",
             "<!-- What you approved, by field. Paste each into its platform; the Profile",
             "     evaluation checks that it is live. Edit freely. -->", ""]
    for key, text in fields.items():
        lines += [f"## {key}", text.strip(), ""]
    path.write_text("\n".join(lines).rstrip() + "\n")
    return path


def approve_field(base_dir, key, text):
    """Set one field in me/persona.md (previous file kept). Returns (old, new)."""
    fields = load_approved(base_dir)
    old = fields.get(key)
    fields[key] = text.strip()
    _write_persona(base_dir, fields)
    return old, fields[key]


def approve_draft(base_dir):
    """Promote me/persona.draft.md to me/persona.md. None when there is no draft."""
    draft = Path(base_dir) / DRAFT
    if not draft.exists():
        return None
    fields = parse_persona(draft.read_text())
    return _write_persona(base_dir, fields) if fields else None


# ---------- approved but not published ----------

def unpublished_findings(base_dir, snaps):
    """A low-severity finding for each approved field not yet on its live profile."""
    from profile_eval import _norm, fingerprint

    approved = load_approved(base_dir)
    by_source = {s["source_key"]: s for s in snaps}
    labels = {f["key"]: f for f in fields_for(snaps)}
    out = []
    for key, text in approved.items():
        spec = labels.get(key)
        snap = by_source.get(spec["source_key"]) if spec else None
        if not spec or not snap:
            continue
        live = " ".join(str(v) for v in snap["identity"].values())
        if _norm(text) in _norm(live):
            continue
        finding = {"dimension": "consistency", "severity": "low",
                   "title": f"Approved {spec['label']} is not on the profile yet",
                   "quote": None, "url": None, "ask": None, "ask_roles": None,
                   "fix": f"Paste the approved text from me/persona.md ({key}) into the profile.",
                   "source_key": spec["source_key"]}
        finding["fingerprint"] = fingerprint(finding)
        out.append(finding)
    return out


# ---------- orchestration ----------

def draft_for_latest(base_dir, llm=None, log=print, now=None):
    """Draft against the latest evaluation and current snapshots. Returns the
    draft path, or None when there is no evaluation yet."""
    from context import load_context
    from llm import LLM
    from store import Store

    base_dir = Path(base_dir)
    store = Store(base_dir)
    ev = store.latest_evaluation()
    if ev is None:
        return None
    snaps = store.latest_snapshots()
    llm = llm or LLM(base_dir)
    drafts = make_drafts(llm, ev["brief"], snaps, store.profile_findings(),
                         load_context(base_dir)["resume"], log=log, now=now)
    store.set_evaluation_drafts(ev["id"], drafts)
    path = base_dir / DRAFT
    path.write_text(render_draft_file(drafts, ev["id"], now=now))
    flagged = sum(1 for d in drafts for o in d["options"] if o["needs_check"] or o["over"])
    log(f"   drafts: {sum(len(d['options']) for d in drafts)} options for {len(drafts)} fields "
        f"({flagged} flagged) -> {DRAFT}")
    return path

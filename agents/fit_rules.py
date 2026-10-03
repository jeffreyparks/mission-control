"""
The rules the models follow, as data the Settings page can show and edit.

Two kinds of standing rule are learned from your own decisions, each as a
reviewable draft that only takes effect once approved:

  learned   me/learned.md           from the roles you pass on or pursue; read by
                                    the job-fit judge (agents/preferences.py)
  guidance  me/profile-guidance.md  from the profile findings you dismiss; read
                                    by the profile reviewer (agents/profile_guidance.py)

Both files are markdown a person can edit by hand: bullets, each followed by an
`<!-- evidence: ... -->` comment, optionally under "## Weigh down" / "## Weigh
up". This module reads them into [{group, text, evidence}], and writes the
approved file back from such a list. The models read only the bullet text (see
context.learned_block / guidance_block), never the comments.

Writing keeps the previous copy as <name>.prev.md, drops the draft's boilerplate
header, and keeps the draft's "already followed" / "considered and dropped"
notes so nothing a reviewer might want is lost. An approved file with no rules
is written empty, so the models see no heading with nothing under it.
"""
import re
from datetime import datetime
from pathlib import Path

LEARNED_GROUPS = ("Weigh down", "Weigh up")
MAX_RULES = 60
MAX_RULE_CHARS = 600
MAX_EVIDENCE_CHARS = 1200

KINDS = {
    "learned": {"approved": "me/learned.md", "draft": "me/learned.draft.md",
                "previous": "me/learned.prev.md", "title": "# Learned preferences",
                "groups": LEARNED_GROUPS},
    "guidance": {"approved": "me/profile-guidance.md", "draft": "me/profile-guidance.draft.md",
                 "previous": "me/profile-guidance.prev.md", "title": "# Profile review guidance",
                 "groups": ()},
}

_BULLET = re.compile(r"^- (.*\S)\s*$")
_EVIDENCE = re.compile(r"^\s+<!--\s*evidence:\s*(.*?)\s*-->\s*$")
_BLOCK = re.compile(r"^<!--(.*?)-->", re.S | re.M)
_DRAFT_HEADER = re.compile(r"DRAFT generated (\d{4}-\d{2}-\d{2}) from ([^\n]*(?:\n\s+[^\n]*)?)")


def _spec(kind):
    if kind not in KINDS:
        raise ValueError(f"not a rule list: {kind!r}")
    return KINDS[kind]


def parse(text, groups=()):
    """Rules and the notes worth keeping, from the markdown of either file.
    Returns ({"rules": [{group, text, evidence}], "notes": [str]})."""
    rules, group, lines = [], None, text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("## "):
            name = line[3:].strip()
            group = name if name in groups else None
            continue
        match = _BULLET.match(line)
        if not match:
            continue
        body = match.group(1)
        # A hand-wrapped bullet continues on indented lines that are not comments.
        while i < len(lines) and lines[i].startswith(" ") and lines[i].strip() \
                and not lines[i].strip().startswith("<!--"):
            body += " " + lines[i].strip()
            i += 1
        evidence = ""
        if i < len(lines):
            ev = _EVIDENCE.match(lines[i])
            if ev:
                evidence, i = ev.group(1), i + 1
        rules.append({"group": group, "text": body, "evidence": evidence})
    if groups:                                   # a bullet with no heading weighs down
        for r in rules:
            r["group"] = r["group"] or groups[0]
    notes = []
    for block in _BLOCK.finditer(text):
        inner = block.group(1).strip()
        if inner.startswith(("DRAFT", "evidence:", "evidence-hash:", "Approved")):
            continue
        notes.append(inner)
    return {"rules": rules, "notes": notes}


def _read_file(base_dir, rel, groups):
    path = Path(base_dir) / rel
    if not path.exists():
        return None
    text = path.read_text()
    return {**parse(text, groups), "text": text, "mtime": path.stat().st_mtime}


def read(base_dir, kind):
    """{"groups", "approved": [rule], "draft": [rule] | None, "draft_date",
    "draft_summary", "draft_notes"} for one kind. Draft rules carry "adopted":
    True when an approved rule already says the same thing."""
    spec = _spec(kind)
    groups = spec["groups"]
    approved = _read_file(base_dir, spec["approved"], groups)
    draft = _read_file(base_dir, spec["draft"], groups)
    have = {r["text"] for r in approved["rules"]} if approved else set()
    out = {"groups": list(groups), "approved": approved["rules"] if approved else [],
           "approved_notes": approved["notes"] if approved else [],
           "draft": None, "draft_date": "", "draft_summary": "", "draft_notes": []}
    if draft:
        head = _DRAFT_HEADER.search(draft["text"])
        out.update(
            draft=[{**r, "adopted": r["text"] in have} for r in draft["rules"]],
            draft_notes=draft["notes"],
            draft_date=head.group(1) if head else datetime.fromtimestamp(draft["mtime"]).strftime("%Y-%m-%d"),
            draft_summary=re.sub(r"\s+", " ", head.group(2)).strip() if head else "")
    return out


def _clean(kind, rules):
    spec = _spec(kind)
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    out = []
    for r in rules:
        if not isinstance(r, dict):
            raise ValueError("each rule must be an object")
        text = re.sub(r"\s+", " ", str(r.get("text") or "")).strip()
        if not text:
            continue                                  # a blank row is just removed
        if len(text) > MAX_RULE_CHARS:
            raise ValueError(f"a rule is {len(text)} characters; the limit is {MAX_RULE_CHARS}")
        if text.startswith("#") or "<!--" in text or "-->" in text:
            raise ValueError("a rule cannot start with '#' or contain a comment marker")
        evidence = re.sub(r"\s+", " ", str(r.get("evidence") or "")).strip().replace("-->", "->")
        group = r.get("group")
        if spec["groups"]:
            group = group if group in spec["groups"] else spec["groups"][0]
        else:
            group = None
        out.append({"group": group, "text": text, "evidence": evidence[:MAX_EVIDENCE_CHARS]})
    if len(out) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} rules")
    return out


def render(kind, rules, notes=()):
    spec = _spec(kind)
    if not rules:
        return ""
    lines = [spec["title"], "",
             "<!-- Approved rules, as edited in Settings. The model reads the bullet text;",
             "     these comments are stripped. -->", ""]
    sections = [(g, [r for r in rules if r["group"] == g]) for g in spec["groups"]] or [(None, rules)]
    for group, items in sections:
        if not items:
            continue
        if group:
            lines += [f"## {group}", ""]
        for r in items:
            lines.append(f"- {r['text']}")
            lines.append(f"  <!-- evidence: {r['evidence'] or 'written by you'} -->")
        lines.append("")
    for note in notes:
        lines += [f"<!-- {note}", "-->", ""]
    return "\n".join(lines).rstrip() + "\n"


def write(base_dir, kind, rules):
    """Replace the approved rules. Returns (old, new) as lists of rule texts and
    groups, the previous file kept as .prev.md."""
    spec = _spec(kind)
    new = _clean(kind, rules)
    base_dir = Path(base_dir)
    path = base_dir / spec["approved"]
    current = _read_file(base_dir, spec["approved"], spec["groups"])
    old = current["rules"] if current else []
    key = lambda rs: [(r["group"], r["text"]) for r in rs]    # noqa: E731
    if key(old) == key(new):
        return old, old
    # Evidence for a rule the page did not send stays what the file had.
    known = {r["text"]: r["evidence"] for r in old}
    new = [{**r, "evidence": r["evidence"] or known.get(r["text"], "")} for r in new]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        (base_dir / spec["previous"]).write_text(path.read_text())
    path.write_text(render(kind, new, current["notes"] if current else ()))
    return old, new


# ---------------------------------------------------------------- what the models see

def _fit_prompt(base_dir):
    """The exact fit-judge prompt prefix, with no roles after it. Built by the
    judge's own code on a stand-in, so the page cannot drift from what runs."""
    from types import SimpleNamespace
    from context import context_block, learned_block
    from job_intel import JobIntel
    stub = SimpleNamespace(ctx=context_block(base_dir), learned=learned_block(base_dir))
    return JobIntel._fit_prefix(stub)


def overview(base_dir, dimension_help=None):
    """The read-only half of the Fit rules section: the fixed rules, how the
    learned ones are drawn from your decisions, and the text each model gets.
    Every value is read from the code or file that applies it."""
    import preferences
    import profile_guidance
    from context import guidance_block
    from job_scanner import _config_path
    from profile_keywords import load_keywords
    from store import CLOSE_REASONS, PREFERENCE_CLOSE_REASONS
    import yaml

    base_dir = Path(base_dir)
    try:
        rules = (yaml.safe_load(_config_path(base_dir, "job-sources.yaml").read_text()) or {}).get("rules") or {}
    except Exception:  # noqa: BLE001 - an unreadable config must not blank the page
        rules = {}
    try:
        prompt = _fit_prompt(base_dir)
    except Exception as exc:  # noqa: BLE001
        prompt = f"(could not be built: {exc})"
    guidance = guidance_block(base_dir)
    count_close = [r for r in CLOSE_REASONS if r in PREFERENCE_CLOSE_REASONS]
    return {
        "fixed": {
            "min_match_score": rules.get("min_match_score"),
            "max_live_roles": rules.get("max_live_roles"),
            "target_keywords": len(load_keywords(base_dir)["target"]),
            "exclude_keywords": len(load_keywords(base_dir)["exclude"]),
        },
        "learning": {
            "window_days": preferences.WINDOW_DAYS,
            "min_decisions": preferences.MIN_DECISIONS,
            "min_leaning": preferences.MIN_LEANING,
            "avoid_ratio": preferences.AVOID_RATIO,
            "seek_ratio": preferences.SEEK_RATIO,
            "min_disagreements": preferences.MIN_DISAGREEMENTS,
            "draft_days": preferences.DRAFT_INTERVAL_DAYS,
            "pursued_statuses": sorted(s.split(" ", 1)[1] for s in preferences.PURSUED_STATUSES),
            "count_close": count_close,
            "ignore_close": [r for r in CLOSE_REASONS if r not in PREFERENCE_CLOSE_REASONS],
            "guidance_min_dismissals": profile_guidance.MIN_DISMISSALS,
            "guidance_min_share": profile_guidance.MIN_DISMISS_SHARE,
        },
        "fit_prompt": prompt,
        "guidance_prompt": guidance,
        "dimensions": dict(dimension_help or {}),
    }

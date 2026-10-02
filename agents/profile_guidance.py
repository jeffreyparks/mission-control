"""
Profile review guidance: what your dismissed findings say the reviewer gets wrong.

A reviewable draft, like learned preferences.

The profile evaluator judges each public profile from scratch, so a finding you
dismissed only stays quiet while its fingerprint matches. A near-duplicate - the
same complaint about another ask, or on another source - comes back as new.
This reads the findings you dismissed, and why, and drafts standing guidance for
the reviewer.

Two halves, deliberately split, as in preferences.py:
  - Python computes the evidence: every dismissal that carries your reason, and
    every group (dimension, dimension on one source, dimension for one ask) you
    dismissed at least MIN_DISMISSALS times and mostly dismissed rather than
    accepted or fixed. A dismissal without a reason counts only toward a group:
    one on its own says nothing.
  - One LLM call reads the candidates, drops the one-offs and the ones another
    candidate covers, and phrases the rest as guidance. The counts in the output
    come from the evidence, never from the model.

The result is a DRAFT (me/profile-guidance.draft.md). Nothing reaches the
evaluator until you approve it into me/profile-guidance.md - with --approve, or
by editing and saving it yourself. --approve keeps the previous copy as
profile-guidance.prev.md.

Dismissed findings stay dismissed, so the evidence never shrinks as the reviewer
learns: a rule cannot erase itself on the next draft. Removing one is your call.

    uv run agents/profile_guidance.py              # write a fresh draft
    uv run agents/profile_guidance.py --evidence   # print the evidence, no LLM call
    uv run agents/profile_guidance.py --approve    # promote the draft
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm import LLM, LLMError, output_budget           # noqa: E402
from store import DB_NAME, Store                       # noqa: E402

DRAFT = "me/profile-guidance.draft.md"
APPROVED = "me/profile-guidance.md"
PREVIOUS = "me/profile-guidance.prev.md"

# A group of dismissals without reasons needs this many to say anything...
MIN_DISMISSALS = 3
# ...and this share of its judged findings dismissed rather than accepted or
# fixed. A group you mostly upheld is the reviewer being right.
MIN_DISMISS_SHARE = 0.6

NOTES_PER_CANDIDATE = 5
NOTE_CHARS = 300
VERDICT_TOKENS_PER_CANDIDATE = 300
VERDICT_TOKENS_OVERHEAD = 500

_HASH_LINE = re.compile(r"<!-- evidence-hash: ([0-9a-f]+) -->")


def _source(key):
    return key or "all sources"


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------

def load_findings(store):
    """Every finding you have judged: dismissed, accepted or fixed."""
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, dimension, source_key, severity, title, quote, ask, state, dismiss_note "
            "FROM profile_findings WHERE state IN ('dismissed','accepted','fixed') ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _groups_of(f):
    yield ("dimension", f["dimension"])
    yield ("dimension on source", f"{f['dimension']} on {_source(f['source_key'])}")
    if f.get("ask"):
        yield ("dimension for ask", f"{f['dimension']} for '{f['ask']}'")


def build_evidence(findings):
    """Totals plus the candidates: each dismissal you gave a reason for, and
    each group you dismissed often and mostly."""
    dismissed = [f for f in findings if f["state"] == "dismissed"]
    noted = [f for f in dismissed if (f.get("dismiss_note") or "").strip()]
    evidence = {"dismissed": len(dismissed), "with_reason": len(noted),
                "upheld": len(findings) - len(dismissed), "candidates": []}

    groups = {}
    for f in findings:
        for key in _groups_of(f):
            groups.setdefault(key, []).append(f)

    def add(candidate):
        candidate["id"] = f"c{len(evidence['candidates']) + 1}"
        evidence["candidates"].append(candidate)

    for (kind, value), items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out = [f for f in items if f["state"] == "dismissed"]
        if len(out) < MIN_DISMISSALS or len(out) < MIN_DISMISS_SHARE * len(items):
            continue
        add({"kind": "pattern", "group": kind, "value": value,
             "dismissed": len(out), "upheld": len(items) - len(out),
             "titles": [f["title"] for f in out][:NOTES_PER_CANDIDATE],
             "reasons": [f["dismiss_note"][:NOTE_CHARS] for f in out
                         if (f.get("dismiss_note") or "").strip()][:NOTES_PER_CANDIDATE]})

    for f in noted:
        upheld = len([g for g in groups[("dimension on source",
                                        f"{f['dimension']} on {_source(f['source_key'])}")]
                      if g["state"] != "dismissed"])
        add({"kind": "reason", "dimension": f["dimension"], "source": _source(f["source_key"]),
             "ask": f.get("ask"), "severity": f["severity"], "title": f["title"],
             "quote": f.get("quote"), "reason": f["dismiss_note"].strip()[:NOTE_CHARS],
             "upheld_same_dimension_and_source": upheld})

    evidence["hash"] = evidence_hash(dismissed)
    return evidence


def evidence_hash(dismissed):
    """Changes when a dismissal is added, withdrawn or given a different reason."""
    raw = json.dumps(sorted((f["id"], (f.get("dismiss_note") or "").strip()) for f in dismissed))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------

def _prompt(evidence, approved_text):
    return f"""You maintain a short "review guidance" note that a profile reviewer reads before
judging a job candidate's public profiles (LinkedIn, GitHub, BlueSky, a personal
site). The reviewer raises findings - fixable problems a hiring manager would
notice - on these dimensions: coverage (shows what target roles ask for), drift
(off-target content), proof (backs resume claims), register (voice matches the
intended persona), consistency (sources tell one story).

The candidate dismissed some of those findings. This note is built ONLY from
those dismissals, so the reviewer stops raising what the candidate has already
rejected.

GUIDANCE THE CANDIDATE HAS ALREADY APPROVED (may be empty):
{approved_text[:3000] or '(none)'}

OVERALL: {evidence['dismissed']} findings dismissed ({evidence['with_reason']} with a reason),
{evidence['upheld']} accepted or fixed.

CANDIDATES - two kinds:
  "reason":  one dismissed finding and the candidate's own reason for dismissing it.
  "pattern": a group of findings the candidate dismissed repeatedly and mostly
             ("upheld" counts the ones in the group they accepted or fixed).
{json.dumps(evidence['candidates'], indent=2)}

For each candidate decide keep or drop, then write one statement for each kept one.

RULES:
  - A kept statement is a standing rule that generalises the dismissal: what
    kind of finding not to raise, where, and why. "On GitHub, don't judge
    register against the persona - it is a code portfolio, not a voice." It must
    still be true for findings the reviewer has not raised yet.
  - Drop a reason that is about one finding only and does not generalise
    ("already fixed offline", "will do later", "typo is intentional here") -
    say why.
  - Drop a candidate another candidate covers. When a pattern and a reason say
    the same thing, keep the one that states it best and drop the other. Never
    keep two statements that say the same thing.
  - When a candidate says what an approved rule already says, keep it and reuse
    that rule's wording, so the note does not churn.
  - A pattern without reasons is weaker evidence: keep it only when the titles
    show one clear kind of finding, and then say only what not to raise - do
    not invent a reason.
  - Never write a rule that tells the reviewer to ignore an ask entirely, or a
    whole dimension on every source: the candidate's target roles still set the
    bar. Narrow it to where the dismissals point.
  - No counts or percentages - those are attached separately.
  - Keeping nothing is a correct answer when nothing is clear.

Return ONLY a JSON array, one object per candidate id:
[{{"id": "<candidate id>", "keep": true | false,
   "statement": "<one sentence, when kept>", "why_dropped": "<short clause, when dropped>"}}]"""


def make_validator(evidence):
    ids = {c["id"] for c in evidence["candidates"]}

    def validate(data):
        if not isinstance(data, list) or not data:
            return False
        for item in data:
            if not isinstance(item, dict) or item.get("id") not in ids:
                return False
            if not isinstance(item.get("keep"), bool):
                return False
            if item["keep"] and not (isinstance(item.get("statement"), str)
                                     and item["statement"].strip()):
                return False
        return True

    return validate


def _evidence_line(c):
    if c["kind"] == "pattern":
        return f"{c['group']} = {c['value']}: dismissed {c['dismissed']}, upheld {c['upheld']}"
    where = f"{c['dimension']} on {c['source']}" + (f", ask '{c['ask']}'" if c.get("ask") else "")
    return f"dismissed '{c['title']}' ({where}) because: {c['reason']}"


def _label(c):
    return c["value"] if c["kind"] == "pattern" else f"'{c['title']}'"


def render_draft(evidence, verdicts, now=None):
    now = now or datetime.now()
    by_id = {c["id"]: c for c in evidence["candidates"]}
    kept = [(by_id[v["id"]], v) for v in verdicts if v.get("keep") and v["id"] in by_id]
    dropped = [(by_id[v["id"]], v) for v in verdicts if not v.get("keep") and v["id"] in by_id]

    lines = [
        "# Profile review guidance",
        "",
        f"<!-- DRAFT generated {now:%Y-%m-%d} from {evidence['dismissed']} dismissed findings",
        f"     ({evidence['with_reason']} with a reason; {evidence['upheld']} accepted or fixed).",
        "     Nothing here is used until it is saved as me/profile-guidance.md:",
        "         uv run agents/profile_guidance.py --approve",
        "     Edit freely first - delete a line you disagree with, reword one that is off.",
        "     The profile reviewer reads the bullet text; these comments are stripped. -->",
        f"<!-- evidence-hash: {evidence['hash']} -->",
        "",
    ]
    for c, v in kept:
        lines.append(f"- {v['statement'].strip()}")
        lines.append(f"  <!-- evidence: {_evidence_line(c)} -->")
    if kept:
        lines.append("")
    else:
        lines += ["_No standing rule in your dismissals yet._", ""]
    if dropped:
        lines.append("<!-- considered and dropped:")
        for c, v in dropped:
            lines.append(f"     - {_label(c)}: {(v.get('why_dropped') or 'no reason given').strip()}")
        lines.append("-->")
    return "\n".join(lines).rstrip() + "\n"


def _approved_text(base_dir):
    path = Path(base_dir) / APPROVED
    if not path.exists():
        return ""
    return re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S).strip()


def write_draft(base_dir, llm=None, now=None):
    """Build the evidence, ask for a verdict on each candidate, write the draft.
    Returns the draft path, or None when the model call failed."""
    base_dir = Path(base_dir)
    evidence = build_evidence(load_findings(Store(base_dir)))
    print(f"  profile guidance: {evidence['dismissed']} dismissed finding(s), "
          f"{len(evidence['candidates'])} candidate(s)")
    if not evidence["candidates"]:
        verdicts = []
    else:
        llm = llm or LLM(base_dir)
        try:
            verdicts = llm.complete_json(
                _prompt(evidence, _approved_text(base_dir)),
                tag="profile-guidance", validate=make_validator(evidence),
                max_tokens=output_budget(len(evidence["candidates"]),
                                         per_item=VERDICT_TOKENS_PER_CANDIDATE,
                                         overhead=VERDICT_TOKENS_OVERHEAD))
        except LLMError as exc:
            print(f"  ! profile guidance draft failed, previous draft kept: {exc}")
            return None
    path = base_dir / DRAFT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_draft(evidence, verdicts, now=now))
    print(f"  profile guidance draft -> {path.relative_to(base_dir)}")
    return path


def draft_is_due(base_dir):
    """Due when your dismissals have changed since the last draft - not on a
    timer: with no new dismissal, a new draft could only say the same thing."""
    base_dir = Path(base_dir)
    if not (base_dir / DB_NAME).exists():
        return False, "no database yet"
    dismissed = [f for f in load_findings(Store(base_dir)) if f["state"] == "dismissed"]
    path = base_dir / DRAFT
    if not path.exists():
        return (True, "no draft yet") if dismissed else (False, "nothing dismissed yet")
    match = _HASH_LINE.search(path.read_text())
    if match and match.group(1) == evidence_hash(dismissed):
        return False, "no new dismissals since the last draft"
    return True, "dismissals changed"


def approve(base_dir):
    """Promote the draft to me/profile-guidance.md, keeping the previous copy."""
    base_dir = Path(base_dir)
    draft, approved = base_dir / DRAFT, base_dir / APPROVED
    if not draft.exists():
        raise FileNotFoundError(f"no draft at {DRAFT} - run profile_guidance.py first")
    if approved.exists():
        shutil.copy2(approved, base_dir / PREVIOUS)
    shutil.copy2(draft, approved)
    return approved


def main(argv=None):
    import workspace
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    workspace.add_argument(ap)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--evidence", action="store_true",
                      help="print the evidence and candidates, no LLM call")
    mode.add_argument("--approve", action="store_true",
                      help=f"promote {DRAFT} to {APPROVED}")
    args = ap.parse_args(argv)
    base = workspace.resolve(args.user)

    if args.approve:
        print(f"approved -> {approve(base).relative_to(base)}")
        return 0
    if args.evidence:
        print(json.dumps(build_evidence(load_findings(Store(base))), indent=2))
        return 0
    return 0 if write_draft(base) else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""One-off: fill Close Reason on closed roles from the notes you already wrote.

Close Reason is new. Before it, the only record of why you passed on a role was
free text in Notes ("Went for their other opening instead", "Too many gaps,
and it's on-site"). This reads those notes back and files each closed role
under one of store.CLOSE_REASONS, with one cheap batched LLM call per chunk.

Only closed rows with notes and no Close Reason yet are touched, so the script
is safe to run twice and never overwrites a reason you set yourself. A note the
model cannot place is left blank - blank means "unknown", which is what every
closed row without notes stays as too.

    uv run scripts/backfill_close_reason.py --dry-run
    uv run scripts/backfill_close_reason.py
    uv run scripts/backfill_close_reason.py --user ariel
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "agents"))

import workspace                                      # noqa: E402
from llm import LLM, LLMError, output_budget          # noqa: E402
from store import CLOSE_REASONS, Store                # noqa: E402

ACTOR = "backfill_close_reason"
CHUNK = 40
NOTE_CHARS = 300

# Notes the pipeline writes itself; they say nothing about why you closed a role.
PIPELINE_NOTE_PREFIXES = ("auto-discovered",)


def candidates(store):
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, org, title, notes, outcomes FROM roles "
            "WHERE status='04 Closed' AND (close_reason IS NULL OR close_reason='') "
            "AND notes IS NOT NULL AND TRIM(notes)!='' ORDER BY row_order")]
    return [r for r in rows
            if not r["notes"].strip().lower().startswith(PIPELINE_NOTE_PREFIXES)]


def build_prompt(batch):
    listed = "\n".join(f'  - "{reason}"' for reason in CLOSE_REASONS)
    roles = "\n\n".join(
        f"[{i}] {row['org']} - {row['title']}\n"
        f"NOTE: {row['notes'][:NOTE_CHARS]}"
        + (f"\nOUTCOME: {row['outcomes']}" if row.get("outcomes") else "")
        for i, row in enumerate(batch))
    return f"""A job seeker closed each role below and left a short note. File each
one under the reason the NOTE gives for closing it.

THE REASONS (use the exact text, or "none"):
{listed}

RULES:
  - Judge only from the note. Do not guess from the title or company.
  - "Chose another role at org": the note says they applied to, or preferred, a
    different role at the same company ("went for their other opening instead",
    "weaker than the other analytics role there", "applied to the manager role").
  - "Posting closed": the employer took the posting down ("listing removed").
  - "Not a fit - skills gap": the note cites gaps or requirements they cannot meet.
  - "Not a fit - function" / "- level" / "- industry": the wrong kind of work,
    the wrong seniority, the wrong sector.
  - Terse notes name the dimension that failed: "Industry" or "Wrong sector
    (mining)" -> "Not a fit - industry"; "Skills" -> "Not a fit - skills gap";
    "Role" -> "Not a fit - function"; "Too junior" -> "Not a fit - level".
  - "Other" is only for a real reason to pass that fits none of the above
    ("Culture", "Gut says no"). A note that records no decision at all - a
    question, a reminder, an interview log - is "none".
  - When a note gives two reasons, pick the first one it names.
  - An employer rejection is NOT a reason the seeker closed it. If the note only
    records a rejection or says nothing about why, answer "none".

ROLES:
{roles}

Return ONLY a JSON array, one object per role:
[{{"i": <number in brackets>, "reason": "<one of the reasons>" | "none"}}]"""


def make_validator(n):
    allowed = set(CLOSE_REASONS) | {"none"}

    def validate(data):
        if not isinstance(data, list):
            return False
        good = {item.get("i") for item in data
                if isinstance(item, dict) and isinstance(item.get("i"), int)
                and 0 <= item["i"] < n and item.get("reason") in allowed}
        return len(good) >= max(1, int(n * 0.8))

    return validate


def classify(llm, rows):
    """{role id: reason} for every row the model could place."""
    found = {}
    for start in range(0, len(rows), CHUNK):
        batch = rows[start:start + CHUNK]
        try:
            data = llm.complete_json(build_prompt(batch), tag="close-reason",
                                     validate=make_validator(len(batch)),
                                     max_tokens=output_budget(len(batch), per_item=40))
        except LLMError as exc:
            print(f"  ! chunk {start // CHUNK + 1} failed, left blank: {exc}")
            continue
        for item in data:
            i, reason = item.get("i"), item.get("reason")
            if isinstance(i, int) and 0 <= i < len(batch) and reason in CLOSE_REASONS:
                found[batch[i]["id"]] = reason
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    workspace.add_argument(ap)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    args = ap.parse_args()

    ws = workspace.resolve(getattr(args, "user", None))
    store = Store(ws)
    print(f"workspace: {ws.name}")

    rows = candidates(store)
    if not rows:
        print("no closed roles with notes and no close reason - nothing to do")
        return 0
    print(f"  {len(rows)} closed role(s) with notes and no close reason")

    planned = classify(LLM(ws), rows)
    by_id = {row["id"]: row for row in rows}

    counts = {}
    for reason in planned.values():
        counts[reason] = counts.get(reason, 0) + 1
    for reason in CLOSE_REASONS:
        if counts.get(reason):
            print(f"  {counts[reason]:>5}  {reason}")
    print(f"  {len(planned)} to set, {len(rows) - len(planned)} left blank")

    if args.dry_run:
        for role_id, reason in planned.items():
            note = by_id[role_id]["notes"].replace("\n", " ")[:70]
            print(f"    {reason:<28} {note}")
        print("dry run - nothing written")
        return 0
    if not planned:
        print("nothing to do")
        return 0

    store.backup_db()
    for role_id, reason in planned.items():
        # set_field logs to the change log, so every backfilled reason can be
        # found (actor = backfill_close_reason) and undone.
        store.set_field(role_id, "close_reason", reason, actor=ACTOR)
    print(f"updated {len(planned)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

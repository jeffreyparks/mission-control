#!/usr/bin/env python3
"""
Add one job posting to the tracker from a public URL.

    uv run add_role.py <url> [--org X] [--title Y] [--priority 1|2|3] [--notes "..."]

What it does, in order:
  1. fetches the posting and extracts org / title / location / comp range / JD text
     (agents/job_fetch.py: Greenhouse, Lever, Ashby, LinkedIn, generic HTML)
  2. refuses duplicates, by URL first and then by org+title
  3. backs up the tracker, then appends ONE row:
     Status "01 Open", Source "manual-add", today's Date Opened, plus --priority/--notes
  4. runs exactly ONE fit call, using the same prompt and schema as agents/job_intel.py
     (JobIntel._fit_prompt - there is no second copy of that prompt anywhere)
  5. writes the fit fields back into the row
  6. refreshes artifacts/jobs/intel-*.json and rebuilds artifacts/html/, reusing every
     other role's already-cached verdict. The live board scan is SKIPPED by default
     (--max-live 0) - adding one role should judge that one role, not rescan every board
     and spend a batch of LLM calls on whatever new postings happen to turn up. Pass
     --max-live N to opt into a scan as part of this run.

Nothing is written until the fetch succeeds, so a bad URL leaves no partial row.
The user's manual columns (Role Cat, Priority, Status, Outcomes, Notes) are asserted
unchanged for every pre-existing row.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "agents"))
sys.path.insert(0, str(BASE))

import workspace  # noqa: E402

import pandas as pd  # noqa: E402

from job_fetch import fetch_posting, canonical_url  # noqa: E402
from job_intel import (  # noqa: E402
    MANUAL_COLUMNS,
    JobIntel,
    LLMError,
    _clean,
    _slug,
    _strip_html,
)

JD_LIMIT = 6000


class AddRoleError(RuntimeError):
    """Anything that should stop us before a row is written."""


# ---------------------------------------------------------------- extraction

def extract(url, org=None, title=None):
    """Fetch the posting. --org/--title always win over what was scraped."""
    posting = fetch_posting(url)
    org = _clean(org) or _clean(posting.get("org"))
    title = _clean(title) or _clean(posting.get("title"))

    if posting.get("error") and not title:
        raise AddRoleError(f"could not read the posting: {posting['error']}")

    missing = [name for name, value in (("org", org), ("title", title)) if not value]
    if missing:
        raise AddRoleError(
            f"the page did not give us: {', '.join(missing)}. "
            f"Re-run with --org/--title to set it by hand."
        )

    return {
        "org": org,
        "title": title,
        "url": _clean(posting.get("url")) or canonical_url(url),
        "location": _clean(posting.get("location")),
        "comp_range": _clean(posting.get("comp_range")),
        "jd": _strip_html(posting.get("jd") or "", limit=JD_LIMIT) or None,
        "source": posting.get("source") or "html",
    }


# ---------------------------------------------------------------- duplicates

def find_duplicate(df, rid, url):
    """Return a human message when this posting is already tracked, else None."""
    if df.empty:
        return None
    target = canonical_url(url or "")
    for _idx, row in df.iterrows():
        link = _clean(row.get("Role Link"))
        if target and link and canonical_url(link) == target:
            return (f"already tracked by URL: {_clean(row.get('Org'))} - "
                    f"{_clean(row.get('Title'))} (status {_clean(row.get('Status')) or '?'}). "
                    f"Nothing was written.")
    for _idx, row in df.iterrows():
        existing = _slug(_clean(row.get("Org")) or "Unknown", _clean(row.get("Title")) or "Untitled")
        if existing == rid:
            return (f"already tracked by org+title: {_clean(row.get('Org'))} - "
                    f"{_clean(row.get('Title'))}. Nothing was written.")
    return None


# ---------------------------------------------------------------- tracker write

def append_row(intel, df, posting, priority=None, notes=None):
    """Back up, append one row, return (new dataframe, row index)."""
    today = datetime.now().strftime("%Y-%m-%d")
    row = {
        "Org": posting["org"],
        "Title": posting["title"],
        "Priority": priority,
        "Date Opened": today,
        "Status": "01 Open",
        "Source": "manual-add",
        "Role Link": posting["url"],
        "Range": posting["comp_range"],
        "Notes": notes,
        "Last Updated": today,
    }

    if df.empty:
        new_df = pd.DataFrame([row])
        return new_df, 0

    intel.backup_tracker()
    before = df.copy(deep=True)
    new_df = pd.concat([df, pd.DataFrame([{c: row.get(c) for c in df.columns}])],
                       ignore_index=True)
    assert len(new_df) == len(before) + 1, "append did not add exactly one row"
    _assert_manual_untouched(before, new_df)
    return new_df, len(new_df) - 1


def _assert_manual_untouched(before, after):
    """Manual columns of pre-existing rows must be byte-identical."""
    head = after.iloc[: len(before)]
    for column in MANUAL_COLUMNS:
        if column in before.columns:
            same = before[column].astype(str).equals(head[column].astype(str))
            assert same, f"manual column mutated: {column}"


def write_fit(intel, df, idx, role):
    """Write the fit verdict into the new row only, then save."""
    before = df.copy(deep=True)
    gaps = role.get("top_gaps") or []
    bits = [b for b in (role.get("seniority_read"), role.get("why")) if b]
    if gaps:
        bits.append("Gaps: " + "; ".join(gaps))

    for column, value in (
        ("Fit Score", role.get("fit_score")),
        ("Fit Rationale", " ".join(bits).strip() or None),
        ("Recommendation", role.get("recommendation")),
    ):
        if value is None:
            continue
        if column not in df.columns:
            df[column] = pd.NA
        df.at[idx, column] = value

    _assert_manual_untouched(before, df)
    df.to_excel(intel.tracker_path, index=False)
    return df


# ---------------------------------------------------------------- refresh

def refresh(intel, role):
    """Rebuild intel JSON and HTML, reusing the verdict we already paid for."""
    seed = {role["fit_fingerprint"]: role}
    out = intel.run(seed_fits=seed)
    build = subprocess.run([sys.executable, str(BASE / "render/build.py"),
                            "--user", str(intel.base_dir)],
                           capture_output=True, text=True, cwd=str(BASE))
    print(build.stdout.strip() or build.stderr.strip())
    return out


# ---------------------------------------------------------------- summary

def summarize(role, posting):
    captured = [k for k in ("location", "comp_range", "jd") if posting.get(k)]
    score = role.get("fit_score")
    print("")
    print(f"  Org            {role['org']}")
    print(f"  Title          {role['title']}")
    print(f"  Fit score      {score if score is not None else 'unknown'}")
    print(f"  Recommendation {role.get('recommendation') or 'unknown'}")
    print(f"  Why            {(role.get('why') or 'no rationale returned').split('. ')[0].strip('.')}.")
    print(f"  Source         {posting['source']} (captured: {', '.join(captured) or 'title only'})")
    print(f"  URL            {posting['url']}")
    print("")


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Add one job posting to the tracker from a URL.")
    ap.add_argument("url")
    ap.add_argument("--org", help="override the scraped company name")
    ap.add_argument("--title", help="override the scraped role title")
    ap.add_argument("--priority", type=int, choices=[1, 2, 3], help="your priority for this role")
    ap.add_argument("--notes", help="free text stored in the Notes column")
    workspace.add_argument(ap)
    ap.add_argument("--max-live", type=int, default=0,
                    help="cap on live board postings included in the refresh "
                         "(default 0 = skip the scan entirely; adding one role should not "
                         "trigger a full board rescan and re-judge whatever it turns up)")
    args = ap.parse_args(argv)

    intel = JobIntel(workspace.resolve(args.user), max_live_roles=args.max_live)

    print(f"  fetching {args.url}")
    try:
        posting = extract(args.url, org=args.org, title=args.title)
    except AddRoleError as exc:
        print(f"  ! {exc}")
        return 1

    rid = _slug(posting["org"], posting["title"])
    df = intel.load_tracker()
    duplicate = find_duplicate(df, rid, posting["url"])
    if duplicate:
        print(f"  ! {duplicate}")
        return 1

    # The JD lives beside the tracker: the sheet has no description column, and the
    # verdict is materially better with it. Same store job_intel.py reads on every run.
    cache = intel.load_jd_cache()
    cache[rid] = {
        "url": posting["url"],
        "location": posting["location"],
        "comp_range": posting["comp_range"],
        "jd": posting["jd"],
        "source": posting["source"],
        "added": datetime.now().strftime("%Y-%m-%d"),
    }
    intel.save_jd_cache()

    df, idx = append_row(intel, df, posting, priority=args.priority, notes=args.notes)
    df.to_excel(intel.tracker_path, index=False)
    print(f"  added row {idx + 1}: {posting['org']} - {posting['title']}")

    # Rebuild the role exactly as job_intel.py sees it, so the one call we make here
    # is the same call it would make, and the verdict is reusable by fingerprint.
    role = next(r for r in intel.tracker_roles(intel.load_tracker()) if r["id"] == rid)
    role["fit_fingerprint"] = intel.fingerprint(role)

    try:
        fits = intel.analyze_fit([role])
    except LLMError as exc:
        print(f"  ! fit analysis failed: {exc}. The row is saved; run job_intel to judge it.")
        return 1
    intel.merge_fit(role, fits.get(rid, {}))

    if role.get("fit_score") is None:
        print("  ! the model returned no usable verdict for this role")
    else:
        df = write_fit(intel, df, idx, role)

    summarize(role, posting)
    refresh(intel, role)
    return 0


if __name__ == "__main__":
    sys.exit(main())

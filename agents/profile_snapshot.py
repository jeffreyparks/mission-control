"""
Profile snapshots: what each public profile says, captured and fingerprinted.

The Profile stage replaces the three daily scanner reports. Each source has a
collector (github_scanner, linkedin_scanner, bluesky_scanner) that returns ONE
snapshot in the shape below. The stage stores a snapshot only when its
fingerprint differs from the last one stored for that source, so an unchanged
profile adds nothing - and, from step 3 of the Profile spec, costs no model
calls either.

Snapshot shape:

    {
      "source":      "github" | "linkedin" | "bluesky" | "site",
      "source_key":  "github:jeffreyparks",   # one per account or site
      "captured_at": "2026-09-27T06:00:00",
      "identity":    {"headline": ..., "bio": ..., ...},  # the fields a recruiter reads first
      "items":       [{"kind": "post|reply|repost|repo|position|skill|page|section",
                       "id": ..., "date": "YYYY-MM-DD" | None, "title": ..., "text": ...,
                       "url": ..., "weight": 1.0, "meta": {...}, "signals": {...}}],
      "stats":       {...},                    # counts, last activity, export age
      "hash":        "sha256 hex",
    }

What the fingerprint covers is the point of the design. It covers the WORDS -
identity, and each item's kind, id, date, title, text, url, weight and meta. It
leaves out `captured_at`, `stats` and every item's `signals`: stars, followers,
likes and repost counts drift daily, and a profile that only gained a like has
not changed what it says about you.

    uv run agents/profile_snapshot.py --collect-only    # snapshot now, print what changed
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ITEM_HASHED_KEYS = ("kind", "id", "date", "title", "text", "url", "weight", "meta")


def make_snapshot(source, source_key, identity, items, stats=None):
    """Assemble a snapshot and stamp its fingerprint."""
    snap = {
        "source": source,
        "source_key": source_key,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "identity": {k: v for k, v in (identity or {}).items() if v not in (None, "")},
        "items": [_item(i) for i in items or []],
        "stats": stats or {},
    }
    snap["hash"] = fingerprint(snap)
    return snap


def _item(raw):
    item = {
        "kind": raw.get("kind"),
        "id": raw.get("id"),
        "date": raw.get("date") or None,
        "title": raw.get("title") or "",
        "text": raw.get("text") or "",
        "url": raw.get("url") or None,
        "weight": float(raw.get("weight", 1.0)),
        "meta": raw.get("meta") or {},
        "signals": raw.get("signals") or {},
    }
    return item


def fingerprint(snap):
    """sha256 over what the profile SAYS - never over counts or capture time."""
    body = {
        "source_key": snap["source_key"],
        "identity": snap.get("identity") or {},
        "items": [{k: i.get(k) for k in ITEM_HASHED_KEYS} for i in snap.get("items") or []],
    }
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def snapshot_text(snap):
    """Every word in a snapshot, as one string - identity values, then items."""
    parts = [str(v) for v in (snap.get("identity") or {}).values()]
    for item in snap.get("items") or []:
        parts.append(item.get("title") or "")
        parts.append(item.get("text") or "")
    return "\n".join(p for p in parts if p)


# ---------- the stage ----------

def collectors(base_dir, env=None):
    """(label, zero-arg collect function) for each source configured here.

    A source with no identity configured is left out and reported, so a run
    for one workspace never falls back to scanning someone else's accounts.
    """
    env = os.environ if env is None else env
    from github_scanner import GitHubScanner
    from linkedin_scanner import LinkedInScanner
    from bluesky_scanner import BlueSkyScanner

    found, skipped = [], []
    github_user = (env.get("GITHUB_USERNAME") or "").strip()
    bluesky_handle = (env.get("BLUESKY_HANDLE") or "").strip()

    if github_user:
        found.append(("github", GitHubScanner(github_user, base_dir).collect))
    else:
        skipped.append("github (no GITHUB_USERNAME)")
    found.append(("linkedin", LinkedInScanner(base_dir).collect))
    if bluesky_handle:
        found.append(("bluesky", BlueSkyScanner(bluesky_handle, base_dir).collect))
    else:
        skipped.append("bluesky (no BLUESKY_HANDLE)")
    return found, skipped


def run_profile_stage(base_dir, log=print, env=None):
    """Collect every configured source, store what changed, and summarise.

    One source failing never stops the others. Returns a dict:
        {"changed": [...], "unchanged": [...], "skipped": [...], "failed": [...]}
    """
    from store import Store

    store = Store(base_dir)
    found, skipped = collectors(base_dir, env)
    out = {"changed": [], "unchanged": [], "skipped": list(skipped), "failed": []}

    for label, collect in found:
        try:
            snap = collect()
        except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
            log(f"   {label}: failed - {exc}")
            out["failed"].append(label)
            continue
        if snap is None:
            out["skipped"].append(label)
            continue
        changed, _ = store.save_snapshot_if_changed(snap)
        out["changed" if changed else "unchanged"].append(snap["source_key"])
        log(f"   {snap['source_key']}: {'changed' if changed else 'unchanged'} "
            f"({len(snap['items'])} items)")
        if snap["stats"].get("stale"):
            log(f"   {snap['source_key']}: export is {snap['stats']['age_days']} days old - "
                f"download a fresh one (see me/linkedin/README.md)")

    for reason in skipped:
        log(f"   skipped: {reason}")
    return out


def summary(result):
    parts = [f"{len(result['changed'])} changed", f"{len(result['unchanged'])} unchanged"]
    if result["failed"]:
        parts.append(f"failed: {', '.join(result['failed'])}")
    return "profiles: " + ", ".join(parts)


def main():
    import workspace

    ap = argparse.ArgumentParser(description="Profile snapshots and evaluation.")
    ap.add_argument("--collect-only", action="store_true",
                    help="snapshot every source now - no model calls - and print what changed")
    workspace.add_argument(ap)
    args = ap.parse_args()
    if not args.collect_only:
        ap.error("only --collect-only is available so far; evaluation comes in a later step")

    base = workspace.resolve(args.user)
    workspace.load_env(base)
    result = run_profile_stage(base)
    print(summary(result))
    return 1 if result["failed"] and not (result["changed"] or result["unchanged"]) else 0


if __name__ == "__main__":
    sys.exit(main())

"""
Profile snapshots: what each public profile says, captured and fingerprinted.

The Profile stage replaces the three daily scanner reports. Each source has a
collector (github_scanner, linkedin_scanner, bluesky_scanner, site_scanner)
that returns ONE snapshot in the shape below. The stage stores a snapshot only when its
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
    uv run agents/profile_snapshot.py                   # snapshot, then evaluate if due
    uv run agents/profile_snapshot.py --force --report  # evaluate now, write a markdown copy
    uv run agents/profile_snapshot.py --drafts          # redo draft rewrites for the latest evaluation
    uv run agents/profile_snapshot.py --approve         # me/persona.draft.md -> me/persona.md
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

CONFIG_DEFAULTS = {
    "sites": [],
    "linkedin_stale_days": 30,
    "reevaluate_days": 7,
    "pursued_min_fit": 70,
    "pursued_window_days": 60,
}


def load_config(base_dir):
    """config/profile.yaml - the workspace's copy if it has one, else the repo's.

    Missing keys fall back to CONFIG_DEFAULTS, and each site is normalised to
    {"url", "max_pages"}; a site entry may also be a bare URL string.
    """
    import yaml
    import workspace

    path = workspace.config_path(base_dir, "profile.yaml")
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    config = {**CONFIG_DEFAULTS, **{k: v for k, v in raw.items() if v is not None}}
    sites = []
    for entry in config.get("sites") or []:
        if isinstance(entry, str):
            entry = {"url": entry}
        if isinstance(entry, dict) and entry.get("url"):
            sites.append({"url": str(entry["url"]).strip(),
                          "max_pages": int(entry.get("max_pages") or 0)})
    config["sites"] = sites
    return config


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
    from site_scanner import SiteScanner

    config = load_config(base_dir)

    found, skipped = [], []
    github_user = (env.get("GITHUB_USERNAME") or "").strip()
    bluesky_handle = (env.get("BLUESKY_HANDLE") or "").strip()

    if github_user:
        found.append(("github", GitHubScanner(github_user, base_dir).collect))
    else:
        skipped.append("github (no GITHUB_USERNAME)")
    found.append(("linkedin",
                  LinkedInScanner(base_dir, stale_days=config["linkedin_stale_days"]).collect))
    if bluesky_handle:
        found.append(("bluesky", BlueSkyScanner(bluesky_handle, base_dir).collect))
    else:
        skipped.append("bluesky (no BLUESKY_HANDLE)")
    for site in config["sites"]:
        found.append((f"site {site['url']}",
                      SiteScanner(site["url"], base_dir, max_pages=site["max_pages"]).collect))
    if not config["sites"]:
        skipped.append("sites (none in config/profile.yaml)")
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
        if _older_than_stored(snap, store.latest_snapshot(snap["source_key"])):
            log(f"   {snap['source_key']}: served an older copy than the last snapshot "
                f"(CDN cache) - kept the last snapshot")
            out["unchanged"].append(snap["source_key"])
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


def _older_than_stored(snap, latest):
    """True when a source dates its content (stats["last_modified"]) and this
    read is older than the stored snapshot - a CDN edge still serving the
    previous build. Storing it would undo your edit and bring back findings
    you had already fixed."""
    new = (snap.get("stats") or {}).get("last_modified")
    old = ((latest or {}).get("stats") or {}).get("last_modified")
    return bool(new and old and new < old)


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
    ap.add_argument("--brief", action="store_true",
                    help="print the target brief (only cheap, cached tagging calls) and stop")
    ap.add_argument("--force", action="store_true",
                    help="evaluate now, even if nothing changed")
    ap.add_argument("--report", action="store_true",
                    help="write a markdown copy of the latest evaluation to artifacts/profiles/")
    ap.add_argument("--drafts", action="store_true",
                    help="redo the draft rewrites for the latest evaluation (one model call) and stop")
    ap.add_argument("--approve", action="store_true",
                    help="promote me/persona.draft.md to me/persona.md (previous copy kept) and stop")
    workspace.add_argument(ap)
    args = ap.parse_args()
    if args.collect_only and (args.force or args.brief):
        ap.error("--collect-only makes no model calls; it cannot be combined with --force or --brief")

    base = workspace.resolve(args.user)
    workspace.load_env(base)

    if args.approve:
        from profile_drafts import approve_draft
        out = approve_draft(base)
        print(f"approved: {out} (previous copy in me/persona.prev.md)" if out
              else "nothing to approve - no me/persona.draft.md yet (run mc profile --drafts)")
        return 0 if out else 1

    if args.drafts:
        from profile_drafts import draft_for_latest
        out = draft_for_latest(base)
        print(f"drafts: {out}" if out else "no evaluation yet - run mc profile first")
        return 0 if out else 1

    if args.brief:
        from profile_brief import build_brief, render_brief
        print(render_brief(build_brief(base, load_config(base))))
        return 0

    result = run_profile_stage(base)
    print(summary(result))
    if args.collect_only:
        return 1 if result["failed"] and not (result["changed"] or result["unchanged"]) else 0

    from profile_eval import evaluate, write_report
    evaluation_id, why = evaluate(base, force=args.force)
    print(f"evaluation: {why}" if evaluation_id is None else f"evaluation {evaluation_id} stored ({why})")
    if args.report:
        out = write_report(base)
        print(f"report: {out}" if out else "report: no evaluation yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())

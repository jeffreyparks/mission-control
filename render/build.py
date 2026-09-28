"""
Render Mission Control artifacts to static HTML.

Pages (all share render/theme.css and the render/_nav.html.j2 shell):
  index.html         home / daily brief, pulls both feeds
  content-radar.html weekly content radar   <- artifacts/content/radar-*.json
  job-tracker.html   daily job tracker      <- artifacts/jobs/intel-*.json
  profile.html       public-profile review  <- profile_evaluations in the database
  settings.html      profile settings       <- me/profile.md, .env, config/profile.yaml

Each page always renders from the LATEST json for its feed, so the two agents
can run on different cadences without blocking each other.

Run:  uv run python render/build.py
"""
import hashlib
import json
import shutil
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE = Path(__file__).resolve().parent.parent   # the repo: templates and theme live here
RENDER = BASE / "render"


def _default_base():
    """The workspace to render when a caller does not name one. Never the repo -
    user data lives in workspace/<name>/ now."""
    sys.path.insert(0, str(BASE))
    import workspace
    return workspace.resolve()


def _asset_version():
    """Short content hash of theme.css, appended to the stylesheet link as
    ?v=... - a cache buster.

    Without it a browser happily keeps serving the stylesheet it cached
    yesterday: the page ships a new feature (the theme switch), the script
    runs, the attribute flips, and nothing changes on screen because the
    cached CSS has no rule for it. The filename changing on every edit is what
    makes a stale copy impossible."""
    try:
        return hashlib.sha1((RENDER / "theme.css").read_bytes()).hexdigest()[:8]
    except OSError:
        return "dev"


def _env():
    env = Environment(
        loader=FileSystemLoader(str(RENDER)),
        # The templates are named *.html.j2; select_autoescape matches on the
        # full suffix, so "html" alone left every page unescaped - and the pages
        # show text scraped from job boards and public profiles.
        autoescape=select_autoescape(["html", "html.j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # Recomputed per environment, and dashboard.py builds one per request, so
    # an edit to theme.css is picked up without restarting anything.
    env.globals["asset_v"] = _asset_version()
    return env


def _latest(pattern, folder, base=None):
    base = base or _default_base()
    files = sorted((base / folder).glob(pattern))
    return files[-1] if files else None


def _load(pattern, folder, base=None):
    src = _latest(pattern, folder, base=base or _default_base())
    return json.loads(src.read_text()) if src else None


def _radar_picks(d):
    if not d:
        return []
    picks = list(d["sections"]["01_pillar_picks"]["picks"])
    order = {"primary": 0, "secondary": 1, "supporting": 2}
    picks.sort(key=lambda p: order.get(p.get("tier"), 9))
    return picks


def _median_fit(roles):
    scores = [r["fit_score"] for r in roles if isinstance(r.get("fit_score"), int)]
    return int(round(statistics.median(scores))) if scores else "—"


# ---------------------------------------------------------------- pages

def render_radar(env, d, base=None):
    if not d:
        print("  radar: no json found, skipped")
        return None
    html = env.get_template("radar.html.j2").render(d=d, picks=_radar_picks(d))
    out = (base or _default_base()) / "artifacts/html" / "content-radar.html"
    out.write_text(html)
    return out


def _days_since(iso_day, today):
    try:
        then = datetime.strptime(str(iso_day)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return max((today - then).days, 0)


def _tracker_context(d):
    """Everything the tracker template needs, computed from d["roles"]/d["orgs"].

    Pulled out of render_tracker so dashboard.py can render the exact same page
    against live database values - overlaid onto a copy of d - without writing
    anything to disk.
    """
    roles = d["roles"]
    orgs = d["orgs"]

    # Freshness, as of now - the dashboard re-renders per request, so ages stay
    # current between pipeline runs. A board date is used when there is one;
    # otherwise the day the tracker first saw the role stands in, flagged as
    # an estimate (the posting is at least that old, possibly much older - the
    # first scan of a board imports every posting on it at once). The page
    # shows the estimate but never filters or highlights on it.
    today = datetime.now().date()
    for r in roles:
        day, basis = r.get("date_posted"), "board"
        if not day:
            day, basis = r.get("date_opened"), "first seen"
        age = _days_since(day, today)
        r["posted_on"] = day if age is not None else None
        r["posted_days"] = age
        r["posted_basis"] = basis if age is not None else None

    cat_counts = Counter((r.get("role_cat") or "Unassigned / new find") for r in roles)
    cats = cat_counts.most_common(10)
    cats_max = max((n for _c, n in cats), default=1)

    # Derived from the roles actually being rendered, not the counts baked into
    # the intel json at pipeline-run time. Recommendation is user-editable from
    # the dashboard now, so d["counts"] can go stale between pipeline runs; the
    # roles list itself is what dashboard.py overlays with live database values.
    rec_counts = Counter(r.get("recommendation") for r in roles if r.get("recommendation"))
    recs = {
        "apply": rec_counts.get("apply", 0),
        "research": rec_counts.get("research", 0),
        "skip": rec_counts.get("skip", 0),
    }
    recbars = [("apply", recs["apply"]), ("research", recs["research"]), ("skip", recs["skip"])]

    # Status filter buttons. The tracker's statuses carry a sort prefix ("01 Open"),
    # so sorting on the raw value keeps pipeline order; the prefix is dropped for
    # display. Only statuses actually present get a button.
    status_counts = Counter(r["status"] for r in roles if r.get("status"))
    statuses = [
        {
            "value": value,
            "label": value.split(" ", 1)[1] if " " in value and value.split(" ", 1)[0].isdigit() else value,
            "count": count,
        }
        for value, count in sorted(status_counts.items())
    ]
    best = [o["best_fit_score"] for o in orgs if o.get("best_fit_score") is not None]

    return dict(
        d=d,
        roles=roles,
        orgs=orgs,
        recs=recs,
        recbars=recbars,
        statuses=statuses,
        cats=cats,
        cats_max=cats_max,
        median_fit=_median_fit(roles),
        orgs_with_apply=sum(1 for o in orgs if o["recommendation_mix"].get("apply")),
        total_open=sum(o["open_count"] for o in orgs),
        best_fit=max(best) if best else "—",
    )


def render_tracker(env, d, write=True, base=None):
    """Job tracker page. Consumes the intel-*.json written by agents/job_intel.py.

    Returns (path_or_None, html). Set write=False to get the rendered string
    without touching disk - that is what the live writeback server does, after
    overlaying current database values onto a copy of d.
    """
    if not d:
        print("  tracker: no intel json found, skipped")
        return None, None

    html = env.get_template("tracker.html.j2").render(**_tracker_context(d))
    out = None
    if write:
        out = (base or _default_base()) / "artifacts/html" / "job-tracker.html"
        out.write_text(html)
    return out, html


def _deltas(base=None):
    """Run-over-run movement from agents/history.py. None when history has not run."""
    try:
        sys.path.insert(0, str(BASE / "agents"))
        from history import get_deltas
        return get_deltas(base or _default_base())
    except Exception as exc:  # noqa: BLE001 - the page must render without history
        print(f"  history deltas unavailable: {exc}")
        return None


def render_index(env, intel, radar, base=None):
    """Home page. Degrades cleanly when either feed has not run yet."""
    roles = intel["roles"] if intel else []
    shortlist = [r for r in roles if r.get("recommendation") == "apply"][:6]
    html = env.get_template("index.html.j2").render(
        deltas=_deltas(base=base),
        profile=profile_summary(base=base),
        intel=intel,
        radar=radar,
        picks=_radar_picks(radar),
        top_picks=_radar_picks(radar)[:3],
        shortlist=shortlist,
        median_fit=_median_fit(roles),
        built=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    out = (base or _default_base()) / "artifacts/html" / "index.html"
    out.write_text(html)
    return out


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

PROFILE_DIMENSIONS = ("coverage", "drift", "consistency", "proof", "register", "freshness")
DIMENSION_HELP = {
    "coverage": "What the target roles ask for, shown anywhere public",
    "drift": "How much of your public voice stays on target",
    "consistency": "Headline, bios and taglines telling one story",
    "proof": "Resume claims backed by something visible",
    "register": "Voice matching your intended persona",
    "freshness": "Recent activity (a rule, not a model)",
}


def source_label(key):
    kind, _, rest = (key or "").partition(":")
    if kind == "site":
        from urllib.parse import urlparse
        return urlparse(rest).netloc or rest
    return {"linkedin": "LinkedIn", "github": "GitHub", "bluesky": "BlueSky"}.get(kind, key or "All sources")


def _store(base):
    sys.path.insert(0, str(BASE / "agents"))
    from store import Store
    return Store(base or _default_base())


def profile_summary(base=None):
    """The Home card: weakest dimension, open high-severity findings, date. None
    before the first evaluation."""
    try:
        store = _store(base)
        ev = store.latest_evaluation()
    except Exception as exc:  # noqa: BLE001 - home must render without profile data
        print(f"  profile summary unavailable: {exc}")
        return None
    if not ev:
        return None
    overall = ev["scores"]["overall"]
    scored = [(overall[d], d) for d in PROFILE_DIMENSIONS if overall.get(d) is not None]
    weakest = min(scored)[1] if scored else None
    findings = store.profile_findings()
    return {
        "date": ev["created_at"][:10],
        "weakest": weakest,
        "weakest_score": overall.get(weakest) if weakest else None,
        "open": len(findings),
        "high": sum(1 for f in findings if f["severity"] == "high"),
    }


def profile_context(base=None):
    """Everything profile.html shows, from the database. Shared with dashboard.py."""
    sys.path.insert(0, str(BASE / "agents"))
    from profile_eval import _profile_url, coverage_matrix

    store = _store(base)
    ev = store.latest_evaluation()
    ctx = {"ev": ev, "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "dims": PROFILE_DIMENSIONS, "dim_help": DIMENSION_HELP, "label": source_label}
    if not ev:
        return ctx
    prev = store.previous_evaluation()
    brief = ev["brief"]
    keys = list(ev["scores"]["per_source"])
    snaps = {k: store.get_snapshot(i) for k, i in (ev["snapshot_ids"] or {}).items()}
    snaps = {k: v for k, v in snaps.items() if v}

    rows = []
    for d in PROFILE_DIMENSIONS:
        now = ev["scores"]["overall"].get(d)
        before = (prev["scores"]["overall"].get(d) if prev else None)
        rows.append({"dim": d, "overall": now,
                     "delta": (now - before) if now is not None and before is not None else None,
                     "cells": [ev["scores"]["per_source"][k].get(d) for k in keys]})

    matrix = coverage_matrix(brief, list(snaps.values())) if snaps else {}
    asks = [{**a, "seen": [matrix.get(a["ask"], {}).get(k, False) for k in keys]}
            for a in brief["asks"][:10]]

    all_findings = store.profile_findings(states=None)
    open_by_source = {}
    for f in all_findings:
        if f["state"] in ("open", "accepted"):
            open_by_source.setdefault(f["source_key"], []).append(f)
    cards = []
    for k in keys:
        snap = snaps.get(k) or {}
        stats = snap.get("stats", {})
        cards.append({
            "key": k, "label": source_label(k), "url": _profile_url(snap) if snap else None,
            "identity": snap.get("identity", {}), "item_count": len(snap.get("items", [])),
            "last_activity": stats.get("last_activity"), "captured": (snap.get("captured_at") or "")[:10],
            "stale": stats.get("stale"), "export_date": stats.get("export_date"),
            "findings": open_by_source.get(k, [])[:3],
        })

    banners = []
    for card in cards:
        if card["stale"] and card["key"].startswith("linkedin:"):
            banners.append(f"Your LinkedIn export is from {card['export_date']}, so LinkedIn findings "
                           f"describe your profile as it was then. Download a fresh export into me/linkedin/.")
    persona = brief.get("persona", {}).get("source")
    if persona != "Persona":
        banners.append(f"Voice was judged against {'your Career Positioning section' if persona == 'Career Positioning' else 'a neutral default'}. "
                       f"Add a ## Persona section to me/profile.md to say exactly how you want to come across.")

    from profile_drafts import load_approved
    approved = load_approved(base or _default_base())
    titles = {f["id"]: f["title"] for f in all_findings}
    drafts = [{**d, "approved": approved.get(d["key"]),
               "options": [{**o, "addressed": [titles[i] for i in o.get("addresses", []) if i in titles]}
                           for o in d["options"]]}
              for d in (ev.get("drafts") or [])]

    ctx.update({
        "drafts": drafts,
        "prev": prev, "brief": brief, "keys": keys, "rows": rows, "asks": asks,
        "shares": [(k, (ev["shares"] or {}).get(k)) for k in keys],
        "cards": cards, "findings": all_findings, "banners": banners,
        "counts": {s: sum(1 for f in all_findings if f["state"] == s)
                   for s in ("open", "accepted", "dismissed", "fixed")},
    })
    return ctx


def render_profile(env, write=True, base=None):
    """Profile page. Returns (path_or_None, html)."""
    html = env.get_template("profile.html.j2").render(**profile_context(base))
    out = None
    if write:
        out = (base or _default_base()) / "artifacts/html" / "profile.html"
        out.write_text(html)
    return out, html


def render_settings(env, write=True, base=None):
    """Settings page: the profile fields the dashboard can edit. A static copy
    is read-only; served by dashboard.py it saves. Returns (path_or_None, html)."""
    base = base or _default_base()
    sys.path.insert(0, str(BASE / "agents"))
    import profile_settings

    html = env.get_template("settings.html.j2").render(
        settings=profile_settings.read_all(base), spec=profile_settings.spec(),
        review_limits=profile_settings.REVIEW_NUMBERS, max_sites=profile_settings.MAX_SITES,
        max_site_pages=profile_settings.MAX_SITE_PAGES, workspace_name=Path(base).name)
    out = None
    if write:
        out = base / "artifacts/html" / "settings.html"
        out.write_text(html)
    return out, html


def main():
    import argparse
    sys.path.insert(0, str(BASE))
    import workspace

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    workspace.add_argument(ap)
    args = ap.parse_args()
    base = workspace.resolve(args.user)

    out = base / "artifacts/html"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(RENDER / "theme.css", out / "theme.css")

    env = _env()
    radar = _load("radar-*.json", "artifacts/content", base=base)
    intel = _load("intel-*.json", "artifacts/jobs", base=base)

    tracker_path, _tracker_html = render_tracker(env, intel, base=base)
    profile_path, _profile_html = render_profile(env, base=base)
    settings_path, _settings_html = render_settings(env, base=base)
    written = [p for p in (
        render_index(env, intel, radar, base=base),
        render_radar(env, radar, base=base),
        tracker_path,
        profile_path,
        settings_path,
    ) if p]
    for p in written:
        print(f"rendered {p.relative_to(base)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

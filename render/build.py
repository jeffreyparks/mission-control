"""
Render Mission Control artifacts to static HTML.

Pages (all share render/theme.css and the render/_nav.html.j2 shell):
  index.html         home / daily brief, pulls both feeds
  content-radar.html weekly content radar   <- artifacts/content/radar-*.json
  job-tracker.html   daily job tracker      <- artifacts/jobs/intel-*.json

Each page always renders from the LATEST json for its feed, so the two agents
can run on different cadences without blocking each other.

Run:  uv run python render/build.py
"""
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


def _env():
    return Environment(
        loader=FileSystemLoader(str(RENDER)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


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


def _tracker_context(d):
    """Everything the tracker template needs, computed from d["roles"]/d["orgs"].

    Pulled out of render_tracker so dashboard.py can render the exact same page
    against live database values - overlaid onto a copy of d - without writing
    anything to disk.
    """
    roles = d["roles"]
    orgs = d["orgs"]

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
    written = [p for p in (
        render_index(env, intel, radar, base=base),
        render_radar(env, radar, base=base),
        tracker_path,
    ) if p]
    for p in written:
        print(f"rendered {p.relative_to(base)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

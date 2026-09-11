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

BASE = Path(__file__).resolve().parent.parent
RENDER = BASE / "render"
OUT = BASE / "artifacts/html"


def _env():
    return Environment(
        loader=FileSystemLoader(str(RENDER)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _latest(pattern, folder):
    files = sorted((BASE / folder).glob(pattern))
    return files[-1] if files else None


def _load(pattern, folder):
    src = _latest(pattern, folder)
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

def render_radar(env, d):
    if not d:
        print("  radar: no json found, skipped")
        return None
    html = env.get_template("radar.html.j2").render(d=d, picks=_radar_picks(d))
    out = OUT / "content-radar.html"
    out.write_text(html)
    return out


def render_tracker(env, d):
    """Job tracker page. Consumes the intel-*.json written by agents/job_intel.py."""
    if not d:
        print("  tracker: no intel json found, skipped")
        return None

    roles = d["roles"]
    orgs = d["orgs"]

    cat_counts = Counter((r.get("role_cat") or "Unassigned / new find") for r in roles)
    cats = cat_counts.most_common(10)
    cats_max = max((n for _c, n in cats), default=1)

    recs = {
        "apply": d["counts"].get("apply", 0),
        "research": d["counts"].get("research", 0),
        "skip": d["counts"].get("skip", 0),
    }
    recbars = [("apply", recs["apply"]), ("research", recs["research"]), ("skip", recs["skip"])]
    best = [o["best_fit_score"] for o in orgs if o.get("best_fit_score") is not None]

    html = env.get_template("tracker.html.j2").render(
        d=d,
        roles=roles,
        orgs=orgs,
        recs=recs,
        recbars=recbars,
        cats=cats,
        cats_max=cats_max,
        median_fit=_median_fit(roles),
        orgs_with_apply=sum(1 for o in orgs if o["recommendation_mix"].get("apply")),
        total_open=sum(o["open_count"] for o in orgs),
        best_fit=max(best) if best else "—",
    )
    out = OUT / "job-tracker.html"
    out.write_text(html)
    return out


def _deltas():
    """Run-over-run movement from agents/history.py. None when history has not run."""
    try:
        sys.path.insert(0, str(BASE / "agents"))
        from history import get_deltas
        return get_deltas(BASE)
    except Exception as exc:  # noqa: BLE001 - the page must render without history
        print(f"  history deltas unavailable: {exc}")
        return None


def render_index(env, intel, radar):
    """Home page. Degrades cleanly when either feed has not run yet."""
    roles = intel["roles"] if intel else []
    shortlist = [r for r in roles if r.get("recommendation") == "apply"][:6]
    html = env.get_template("index.html.j2").render(
        deltas=_deltas(),
        intel=intel,
        radar=radar,
        picks=_radar_picks(radar),
        top_picks=_radar_picks(radar)[:3],
        shortlist=shortlist,
        median_fit=_median_fit(roles),
        built=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    out = OUT / "index.html"
    out.write_text(html)
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(RENDER / "theme.css", OUT / "theme.css")

    env = _env()
    radar = _load("radar-*.json", "artifacts/content")
    intel = _load("intel-*.json", "artifacts/jobs")

    written = [p for p in (
        render_index(env, intel, radar),
        render_radar(env, radar),
        render_tracker(env, intel),
    ) if p]
    for p in written:
        print(f"rendered {p.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

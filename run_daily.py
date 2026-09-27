"""
Mission Control runner.

Cadences:
  daily   - profile snapshots (+ evaluation when due), job intel, HTML build
  weekly  - content radar (runs when the newest radar is >= 7 days old)
  weekly  - learned-preferences draft, me/learned.draft.md (>= 7 days old);
            it is only a draft - nothing changes until you approve it

Job roles are judged by Job Intel (LLM fit analysis) by default. Intel sends only
NEW or EDITED roles to the model and reuses stored verdicts for everything else,
so a normal day costs close to nothing.

Usage:
  uv run run_daily.py                 # respect cadences
  uv run run_daily.py --force-radar   # run the radar regardless of cadence
  uv run run_daily.py --only jobs     # one stage: profiles|jobs|radar|preferences|history|render
  uv run run_daily.py --radar-only    # radar now + rebuild pages, no job scan
  uv run run_daily.py --profile-only  # profile snapshots now + rebuild pages, no job scan
  uv run run_daily.py --refresh-intel # re-judge every role from scratch (full price)
  uv run run_daily.py --refresh-open  # re-judge only roles not yet closed
  uv run run_daily.py --no-intel      # legacy keyword JobScanner instead of intel
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent          # the repo: code, templates, app config
sys.path.insert(0, str(BASE / "agents"))
sys.path.insert(0, str(BASE))
import workspace  # noqa: E402

# Machine-wide secrets now; the workspace's own identity is layered on top in
# main(), once --user has been parsed. Loading identity at import time would
# bind whoever the repo .env named, whatever workspace was asked for.
load_dotenv(BASE / ".env")

# The workspace every stage reads and writes. Set once in main() from --user, so
# no stage can accidentally touch a different person's data mid-run.
WS = None


def ws():
    global WS
    if WS is None:
        WS = workspace.resolve()
    return WS

RADAR_INTERVAL_DAYS = 7

# Set from CLI flags in main(); read by run_jobs().
OPTS = {"intel": True, "refresh": False, "refresh_open": False, "max_live": None}   # None = this workspace's config


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def stage(name, fn):
    """Run one stage. Never let a single failure stop the run."""
    try:
        log(f"-> {name}")
        result = fn()
        log(f"   ok: {result}" if result else f"   ok: {name}")
        return True
    except Exception as exc:  # noqa: BLE001 - stages must be independent
        log(f"   FAILED {name}: {exc}")
        return False


def log_routing():
    """One line per routed LLM call, so every run records where inference went."""
    try:
        import routing
    except Exception:  # noqa: BLE001
        return
    for tag in ("job-fit", "role-cat", "role-function", "org-sectors", "content-radar", "preferences",
                "profile-asks", "profile-ask-merge", "profile-claims", "profile-eval-source", "profile-eval-synth"):
        ladder = routing.ladder_for_tag(tag)
        target = ladder[0].split("/", 1)[-1] if ladder else "claude CLI default"
        log(f"   route: {tag:14s} -> {target}")


def radar_is_due():
    files = sorted((ws() / "artifacts/content").glob("radar-*.json"))
    if not files:
        return True, "no radar yet"
    try:
        last = datetime.strptime(json.loads(files[-1].read_text())["date"], "%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return True, "unreadable radar date"
    age = (datetime.now() - last).days
    if age >= RADAR_INTERVAL_DAYS:
        return True, f"{age}d since last radar"
    return False, f"{RADAR_INTERVAL_DAYS - age}d until next radar"


# ---------- stages ----------

def run_profiles():
    from profile_eval import evaluate
    from profile_snapshot import run_profile_stage, summary
    line = summary(run_profile_stage(ws(), log=log))
    evaluation_id, why = evaluate(ws(), log=log)
    return f"{line}; evaluation " + (f"{evaluation_id} ({why})" if evaluation_id else f"skipped: {why}")


def run_jobs():
    if not OPTS["intel"]:
        from job_scanner import JobScanner
        out = JobScanner(ws()).run()
        return f"legacy scan: {Path(out).name}" if out else "legacy scan: no output"

    from job_intel import JobIntel
    out = JobIntel(ws(), refresh=OPTS["refresh"], refresh_open=OPTS["refresh_open"],
                   max_live_roles=OPTS["max_live"]).run()
    return f"job intel: {Path(out).name}" if out else "job intel: no output"


def run_radar():
    from content_radar import ContentRadar
    out = ContentRadar(ws(), github_user=os.environ.get("GITHUB_USERNAME", "").strip()).run()
    return f"radar: {Path(out).name}" if out else "radar: no output"


def run_preferences():
    from preferences import write_draft
    out = write_draft(ws())
    return (f"preferences draft: {out.name} - review, then "
            f"uv run agents/preferences.py --approve") if out else "preferences: no draft"


def run_history():
    from history import History
    out = History(ws()).run()
    return f"history: {Path(out).name}" if out else "history: no output"


def run_render():
    sys.path.insert(0, str(BASE / "render"))
    import build
    base = ws()
    env = build._env()
    (base / "artifacts/html").mkdir(parents=True, exist_ok=True)
    shutil.copy2(build.RENDER / "theme.css", base / "artifacts/html/theme.css")
    radar = build._load("radar-*.json", "artifacts/content", base=base)
    intel = build._load("intel-*.json", "artifacts/jobs", base=base)
    tracker_path, _html = build.render_tracker(env, intel, base=base)
    build.render_index(env, intel, radar, base=base)
    build.render_radar(env, radar, base=base)
    build.render_profile(env, base=base)
    return "html rebuilt"


STAGES = {
    "profiles": run_profiles,
    "jobs": run_jobs,
    "radar": run_radar,
    "preferences": run_preferences,
    "history": run_history,
    "render": run_render,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--force-radar", action="store_true",
                    help="run the content radar even if it is not due")
    ap.add_argument("--only", choices=sorted(STAGES), help="run a single stage")
    ap.add_argument("--radar-only", action="store_true",
                    help="run the content radar now and rebuild the pages - no job scan")
    ap.add_argument("--profile-only", action="store_true",
                    help="snapshot the public profiles now and rebuild the pages - no job scan")
    intel = ap.add_mutually_exclusive_group()
    intel.add_argument("--intel", dest="intel", action="store_true", default=True,
                       help="LLM fit analysis for job roles (default)")
    intel.add_argument("--no-intel", dest="intel", action="store_false",
                       help="fall back to the legacy keyword JobScanner")
    refresh = ap.add_mutually_exclusive_group()
    refresh.add_argument("--refresh-intel", action="store_true",
                         help="re-judge every role instead of reusing unchanged verdicts")
    refresh.add_argument("--refresh-open", action="store_true",
                         help="re-judge every role not yet closed; closed roles keep their verdicts")
    ap.add_argument("--max-live", type=int, default=None,
                     help="cap on live postings judged this run "
                          "(default: rules.max_live_roles in job-sources.yaml, else 200)")
    workspace.add_argument(ap)
    args = ap.parse_args()
    exclusive = [flag for flag, on in (("--only", args.only), ("--radar-only", args.radar_only),
                                       ("--profile-only", args.profile_only)) if on]
    if len(exclusive) > 1:
        ap.error(f"{' and '.join(exclusive)} cannot be combined")

    global WS
    WS = workspace.resolve(args.user)
    workspace.load_env(WS)          # identity for THIS workspace, overriding the repo .env
    OPTS["intel"] = args.intel
    OPTS["refresh"] = args.refresh_intel
    OPTS["refresh_open"] = args.refresh_open
    OPTS["max_live"] = args.max_live

    log(f"Mission Control run started  (workspace: {WS.name})")
    mode = " (refresh)" if args.refresh_intel else " (refresh open roles)" if args.refresh_open else ""
    log(f"   jobs: intel{mode}" if args.intel
        else "   jobs: legacy keyword scan")
    log_routing()

    if args.only:
        ok = stage(args.only, STAGES[args.only])
        log("done")
        return 0 if ok else 1

    if args.radar_only:
        # History is skipped on purpose: it snapshots the day's jobs, and with
        # no job scan it would record a misleading "nothing changed" day.
        ok = stage("content radar (radar only)", run_radar)
        stage("static html", run_render)
        log("done")
        return 0 if ok else 1

    if args.profile_only:
        # History is skipped for the same reason as --radar-only.
        ok = stage("profile snapshots (profile only)", run_profiles)
        stage("static html", run_render)
        log("done")
        return 0 if ok else 1

    stage("profile snapshots (daily)", run_profiles)
    stage("job intel (daily)", run_jobs)

    due, why = radar_is_due()
    if due or args.force_radar:
        stage(f"content radar (weekly - {why})", run_radar)
    else:
        log(f"-> content radar skipped ({why})")

    from preferences import draft_is_due
    due, why = draft_is_due(ws())
    if due:
        stage(f"learned-preferences draft (weekly - {why})", run_preferences)
    else:
        log(f"-> learned-preferences draft skipped ({why})")

    stage("history snapshot + deltas (daily)", run_history)
    stage("static html", run_render)

    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

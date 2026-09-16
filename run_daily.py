"""
Mission Control runner.

Cadences:
  daily   - profile scanners, job intel, HTML build
  weekly  - content radar (runs when the newest radar is >= 7 days old)

Job roles are judged by Job Intel (LLM fit analysis) by default. Intel sends only
NEW or EDITED roles to the model and reuses stored verdicts for everything else,
so a normal day costs close to nothing.

Usage:
  uv run run_daily.py                 # respect cadences
  uv run run_daily.py --force-radar   # run the radar regardless of cadence
  uv run run_daily.py --only jobs     # one stage: profiles|jobs|radar|history|render
  uv run run_daily.py --refresh-intel # re-judge every role from scratch (full price)
  uv run run_daily.py --no-intel      # legacy keyword JobScanner instead of intel
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "agents"))
load_dotenv(BASE / ".env")

RADAR_INTERVAL_DAYS = 7

# Set from CLI flags in main(); read by run_jobs().
OPTS = {"intel": True, "refresh": False}


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
    for tag in ("job-fit", "role-cat", "org-sectors", "content-radar"):
        ladder = routing.ladder_for_tag(tag)
        target = ladder[0].split("/", 1)[-1] if ladder else "claude CLI default"
        log(f"   route: {tag:14s} -> {target}")


def radar_is_due():
    files = sorted((BASE / "artifacts/content").glob("radar-*.json"))
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
    from profile_scanner import GitHubScanner
    from linkedin_scanner import LinkedInScanner
    from bluesky_scanner import BlueSkyScanner

    github_user = os.environ.get("GITHUB_USERNAME", "").strip()
    bluesky_handle = os.environ.get("BLUESKY_HANDLE", "").strip()

    runners = [("linkedin", lambda: LinkedInScanner(BASE).run())]
    if github_user:
        runners.insert(0, ("github", lambda: GitHubScanner(github_user, BASE).run()))
    else:
        log("   github scanner skipped (GITHUB_USERNAME not set in .env)")
    if bluesky_handle:
        runners.append(("bluesky", lambda: BlueSkyScanner(bluesky_handle, BASE).run()))
    else:
        log("   bluesky scanner skipped (BLUESKY_HANDLE not set in .env)")

    done = []
    for label, runner_fn in runners:
        try:
            if runner_fn():
                done.append(label)
        except Exception as exc:  # noqa: BLE001
            log(f"   {label} scanner failed: {exc}")
    return f"profiles: {', '.join(done) or 'none'}"


def run_jobs():
    if not OPTS["intel"]:
        from job_scanner import JobScanner
        out = JobScanner(BASE).run()
        return f"legacy scan: {Path(out).name}" if out else "legacy scan: no output"

    from job_intel import JobIntel
    out = JobIntel(BASE, refresh=OPTS["refresh"]).run()
    return f"job intel: {Path(out).name}" if out else "job intel: no output"


def run_radar():
    from content_radar import ContentRadar
    out = ContentRadar(BASE).run()
    return f"radar: {Path(out).name}" if out else "radar: no output"


def run_history():
    from history import History
    out = History(BASE).run()
    return f"history: {Path(out).name}" if out else "history: no output"


def run_render():
    sys.path.insert(0, str(BASE / "render"))
    import build
    build.main()
    return "html rebuilt"


STAGES = {
    "profiles": run_profiles,
    "jobs": run_jobs,
    "radar": run_radar,
    "history": run_history,
    "render": run_render,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--force-radar", action="store_true",
                    help="run the content radar even if it is not due")
    ap.add_argument("--only", choices=sorted(STAGES), help="run a single stage")
    intel = ap.add_mutually_exclusive_group()
    intel.add_argument("--intel", dest="intel", action="store_true", default=True,
                       help="LLM fit analysis for job roles (default)")
    intel.add_argument("--no-intel", dest="intel", action="store_false",
                       help="fall back to the legacy keyword JobScanner")
    ap.add_argument("--refresh-intel", action="store_true",
                    help="re-judge every role instead of reusing unchanged verdicts")
    args = ap.parse_args()

    OPTS["intel"] = args.intel
    OPTS["refresh"] = args.refresh_intel

    log("Mission Control run started")
    log(f"   jobs: {'intel' + (' (refresh)' if args.refresh_intel else '')}" if args.intel
        else "   jobs: legacy keyword scan")
    log_routing()

    if args.only:
        ok = stage(args.only, STAGES[args.only])
        log("done")
        return 0 if ok else 1

    stage("profile scanners (daily)", run_profiles)
    stage("job intel (daily)", run_jobs)

    due, why = radar_is_due()
    if due or args.force_radar:
        stage(f"content radar (weekly - {why})", run_radar)
    else:
        log(f"-> content radar skipped ({why})")

    stage("history snapshot + deltas (daily)", run_history)
    stage("static html", run_render)

    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

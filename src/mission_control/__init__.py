"""Mission Control CLI.

    mc setup       scaffold me/, fill in the basics, first run
    mc run         run the daily pipeline
    mc render      rebuild the static dashboard from existing data
    mc dashboard   editable local dashboard (background by default)
    mc users       list workspaces
    mc help        show this list of commands

Every command takes --user NAME to run against workspace/NAME instead of
workspace/default, so testing someone else's data never touches your own.

Thin wrapper around the repo's top-level scripts (setup.py, run_daily.py,
dashboard.py). The underlying scripts still work standalone
(`uv run run_daily.py`, etc.) - `mc` is additive, not a replacement. Every
argument after the subcommand passes straight through to that script's own
argparse, so `mc run --only jobs` behaves exactly like
`uv run run_daily.py --only jobs`. `mc <subcommand> --help` shows that
script's real, full option list.

Install `mc` globally with `uv tool install --editable .`, or use it with
zero install from inside the repo via `uv run mc <subcommand>`.
"""
import argparse
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / ".mc"
DASHBOARD_PID = RUNTIME_DIR / "dashboard.pid"
DASHBOARD_LOG = RUNTIME_DIR / "dashboard.log"

SCRIPTS = {
    "setup": "setup",
    "run": "run_daily",
    "dashboard": "dashboard",
    "render": "render/build",
}

HELP_TEXT = """\
mc setup [--status] [--dry-run]
    scaffold me/, fill in the basics, first run
    --status    what's configured, no prompts, no LLM calls
    --dry-run   preview scan scope before spending anything

mc run [--force-radar] [--only STAGE] [--refresh-intel] [--no-intel]
    run the daily pipeline
    --force-radar    run the weekly content radar now
    --only STAGE     one stage: profiles | jobs | radar | history | render
    --refresh-intel  re-judge every role instead of reusing cached verdicts
    --no-intel       legacy keyword scanner instead of LLM fit

mc render [--user NAME]
    rebuild the static dashboard pages from that workspace's existing data
    (no scans, no LLM calls)

mc users
    list the workspaces under workspace/

mc dashboard [--host HOST] [--port PORT] [--foreground]
mc dashboard stop
    editable local dashboard - starts in the background by default so it
    does not tie up this terminal; `mc dashboard stop` shuts it down
    --host HOST      default 127.0.0.1
    --port PORT      default 8787
    --foreground     block this terminal instead (old behavior, live output)

mc help
    show this list of commands

--user NAME works on setup, run, render and dashboard. It selects
workspace/NAME - one person's entire dataset (artifacts, data, me, config).
Defaults to $MC_WORKSPACE, then "default".

Full detail for any subcommand's own options: mc <subcommand> --help
"""


def _run_script(script_name, argv):
    """Load a repo-root script by file path and call its main() with sys.argv
    rewritten to exactly the args passed after the mc subcommand. Loading by
    file path (not `import`) keeps these scripts working as standalone
    entry points too - they are not part of the installed package."""
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "agents"))

    path = REPO_ROOT / f"{script_name}.py"
    module_name = script_name.replace("/", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)

    old_argv = sys.argv
    sys.argv = [f"{path.name}", *argv]
    try:
        spec.loader.exec_module(module)  # the script's own `if __name__ ==
        # "__main__"` guard does not fire here - exec_module gives it the
        # module name we chose above, not "__main__" - so main() below is
        # the only call, never a double run.
        if hasattr(module, "main"):
            return module.main() or 0
        return 0
    finally:
        sys.argv = old_argv


# ---------- dashboard: background start/stop ----------

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _read_pid():
    if not DASHBOARD_PID.exists():
        return None
    try:
        pid = int(DASHBOARD_PID.read_text().strip())
    except ValueError:
        DASHBOARD_PID.unlink(missing_ok=True)
        return None
    if not _pid_alive(pid):
        DASHBOARD_PID.unlink(missing_ok=True)
        return None
    return pid


def _dashboard_host_port(argv):
    """Peek at --host/--port for the printed URL only - the real parsing
    happens in dashboard.py itself, in the subprocess we spawn below."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    known, _ = p.parse_known_args(argv)
    return known.host, known.port


def _dashboard_stop():
    pid = _read_pid()
    if not pid:
        print("dashboard is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    DASHBOARD_PID.unlink(missing_ok=True)
    print(f"dashboard stopped (pid {pid})")
    return 0


def _dashboard_start(argv, foreground):
    if foreground:
        return _run_script("dashboard", argv)

    existing = _read_pid()
    host, port = _dashboard_host_port(argv)
    if existing:
        print(f"dashboard already running (pid {existing}) - http://{host}:{port}")
        return 0

    RUNTIME_DIR.mkdir(exist_ok=True)
    log = open(DASHBOARD_LOG, "a")
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "dashboard.py"), *argv],
        cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,  # detach from this terminal/session
    )
    DASHBOARD_PID.write_text(str(proc.pid))

    time.sleep(1)
    if not _pid_alive(proc.pid):
        DASHBOARD_PID.unlink(missing_ok=True)
        print(f"dashboard failed to start - see {DASHBOARD_LOG}")
        return 1

    print(f"dashboard running in the background (pid {proc.pid})")
    print(f"  http://{host}:{port}")
    print(f"  log: {DASHBOARD_LOG.relative_to(REPO_ROOT)}")
    print("  mc dashboard stop   to shut it down")
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="mc",
        description="Mission Control: career operations automation.",
    )
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", add_help=False,
                    help="scaffold me/, fill in the basics, first run")
    sub.add_parser("run", add_help=False,
                    help="run the daily pipeline")
    sub.add_parser("render", add_help=False,
                    help="rebuild static dashboard pages from existing data")
    sub.add_parser("users", add_help=False, help="list workspaces")
    sub.add_parser("dashboard", add_help=False,
                    help="editable local dashboard (background by default)")
    sub.add_parser("help", add_help=False,
                    help="show this list of commands")

    args, remainder = ap.parse_known_args()

    if args.command == "help":
        print(HELP_TEXT)
        return 0

    if args.command == "users":
        sys.path.insert(0, str(REPO_ROOT))
        import workspace
        names = workspace.available()
        current = os.environ.get("MC_WORKSPACE") or workspace.DEFAULT
        for n in names:
            print(f"{'*' if n == current else ' '} {n}")
        if not names:
            print("no workspaces yet - run: mc setup")
        return 0

    if args.command == "dashboard":
        if remainder and remainder[0] == "stop":
            return _dashboard_stop()
        foreground = "--foreground" in remainder
        argv = [a for a in remainder if a != "--foreground"]
        return _dashboard_start(argv, foreground)

    return _run_script(SCRIPTS[args.command], remainder)


if __name__ == "__main__":
    sys.exit(main())

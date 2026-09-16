"""Mission Control CLI.

    mc setup       scaffold me/, fill in the basics, first run
    mc run         run the daily pipeline
    mc dashboard   editable local dashboard

Thin wrapper around the repo's top-level scripts (setup.py, run_daily.py,
dashboard.py). The underlying scripts still work standalone
(`uv run run_daily.py`, etc.) - `mc` is additive, not a replacement. Every
argument after the subcommand passes straight through to that script's own
argparse, so `mc run --only jobs` behaves exactly like
`uv run run_daily.py --only jobs`.

Install `mc` globally with `uv tool install --editable .`, or use it with
zero install from inside the repo via `uv run mc <subcommand>`.
"""
import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SCRIPTS = {
    "setup": "setup",
    "run": "run_daily",
    "dashboard": "dashboard",
}


def _run_script(script_name, argv):
    """Load a repo-root script by file path and call its main() with sys.argv
    rewritten to exactly the args passed after the mc subcommand. Loading by
    file path (not `import`) keeps these scripts working as standalone
    entry points too - they are not part of the installed package."""
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "agents"))

    path = REPO_ROOT / f"{script_name}.py"
    spec = importlib.util.spec_from_file_location(script_name, path)
    module = importlib.util.module_from_spec(spec)

    old_argv = sys.argv
    sys.argv = [f"{script_name}.py", *argv]
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
    sub.add_parser("dashboard", add_help=False,
                    help="editable local dashboard")

    args, remainder = ap.parse_known_args()
    return _run_script(SCRIPTS[args.command], remainder)


if __name__ == "__main__":
    sys.exit(main())

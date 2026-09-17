"""
Workspace resolution for Mission Control.

All of one person's data - scans, analysis, database, profile, rendered pages -
lives in a single directory under workspace/, so a whole dataset is one
swappable unit:

    workspace/
      default/                 <- your real data (the default workspace)
        artifacts/{jobs,content,profiles,history,html}
        data/{mission-control.db,history.db,llm-cache}
        me/{profile.md,resume.txt,linkedin/}
        config/                <- optional per-workspace overrides
      ariel/                   <- someone else's data, same shape

Every entry point takes --user/-u NAME (or the MC_WORKSPACE env var) and runs
against workspace/NAME. Nothing outside that directory is touched, so testing
against another dataset can never disturb your own.

Code, templates and app config stay in the repo. Only user data moves.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent
ROOT = REPO / "workspace"
DEFAULT = "default"

# Created on demand so a brand new workspace is immediately usable.
SUBDIRS = (
    "artifacts/jobs",
    "artifacts/content",
    "artifacts/profiles",
    "artifacts/history",
    "artifacts/html",
    "data",
    "me",
    "config",
)


def resolve(name=None, ensure_dirs=True):
    """workspace/NAME for a bare name, or the path itself if NAME looks like a
    path. Falls back to $MC_WORKSPACE, then 'default'."""
    name = name or os.environ.get("MC_WORKSPACE") or DEFAULT
    p = Path(name).expanduser()
    # A bare name ("ariel") means workspace/ariel. Anything with a separator, or
    # an absolute path, is taken literally - handy for a temp dir in a test.
    ws = p.resolve() if (p.is_absolute() or len(p.parts) > 1) else ROOT / name
    if ensure_dirs:
        ensure(ws)
    return ws


def ensure(ws):
    """Make the workspace skeleton. Safe to call repeatedly."""
    for sub in SUBDIRS:
        (ws / sub).mkdir(parents=True, exist_ok=True)
    return ws


def add_argument(parser):
    """The one flag every entry point shares."""
    parser.add_argument(
        "-u", "--user", "--workspace", dest="user", default=None, metavar="NAME",
        help=f"workspace to operate on: workspace/NAME, or a path. "
             f"Defaults to $MC_WORKSPACE or {DEFAULT!r}. Each workspace holds one "
             f"person's data, so switching users can never touch another's.",
    )


def config_path(ws, filename):
    """Per-workspace config if present, else the repo's tracked default. Lets a
    new workspace work with zero setup while still allowing an override."""
    local = Path(ws) / "config" / filename
    return local if local.exists() else REPO / "config" / filename


def available():
    """Existing workspace names, for listings and error messages."""
    if not ROOT.exists():
        return []
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith("."))

# Keys that belong to a PERSON, not to this machine. They live in
# workspace/<name>/.env so switching --user switches identity too - a run for
# one person can never scan another person's accounts. Everything else
# (LLM_BACKEND, ANTHROPIC_API_KEY, OPENROUTER_API_KEY) is machine-wide and
# stays in the repo-root .env, shared by every workspace.
IDENTITY_KEYS = (
    "GITHUB_USERNAME",
    "BLUESKY_HANDLE",
    "BLUESKY_APP_PASSWORD",
    "BLUESKY_PDS_URL",
)


def env_path(ws):
    """Where one workspace's identity lives."""
    return Path(ws) / ".env"


def load_env(ws=None):
    """Machine secrets first, then this workspace's identity on top.

    Called once an entry point knows which workspace it is running against. The
    workspace file wins, so a workspace with no .env simply has no identity and
    the profile scanners skip themselves - which is safer than silently falling
    back to whoever ran last.
    """
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
    if ws is not None:
        load_dotenv(env_path(ws), override=True)
    return ws


def read_env(path, key):
    """One value from a .env file, without importing it into os.environ."""
    path = Path(path)
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def set_env(path, key, value):
    """Upsert one key in a .env file, creating the file if needed."""
    path = Path(path)
    lines = path.read_text().splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")

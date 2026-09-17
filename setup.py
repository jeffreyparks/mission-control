#!/usr/bin/env python3
"""
Mission Control setup wizard.

One command to go from a fresh checkout to your first real run:

    uv run setup.py               # interactive: scaffold me/, fill in the basics
    uv run setup.py --status      # what's configured, no prompts, no LLM calls
    uv run setup.py --dry-run     # preview what a real run would scan, no LLM calls

me/ is your single input folder (resume, career goals, optional LinkedIn export).
.env holds secrets (API keys, BlueSky app password) - never me/. Both are
gitignored; only templates/me/ and .env.example are committed, so a fresh
checkout always has a scaffold to copy from.

Re-run any time to add or edit something; it will not overwrite files you
already filled in.
"""
import argparse
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import workspace  # noqa: E402

WS = workspace.resolve()          # rebound in main() once --user is parsed
ME = WS / "me"
TEMPLATES = BASE / "templates" / "me"
ENV_PATH = BASE / ".env"
ENV_EXAMPLE = BASE / ".env.example"

PLACEHOLDER_MARKERS = ("<!--", "Replace this file")


def prompt(msg, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{msg}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def yes(msg, default=False):
    d = "Y/n" if default else "y/N"
    try:
        val = input(f"{msg} ({d}): ").strip().lower()
    except EOFError:
        val = ""
    if not val:
        return default
    return val.startswith("y")


# ---------- scaffold ----------

def scaffold():
    """Create this workspace's me/, config/ and .env from the repo templates if
    they do not exist yet. Never overwrites a file the user already has."""
    workspace.ensure(WS)
    ME.mkdir(parents=True, exist_ok=True)
    (ME / "linkedin").mkdir(exist_ok=True)
    created = []
    for src in TEMPLATES.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(TEMPLATES)
        dest = ME / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            created.append(str(rel))

    # Seed the workspace's own config from the project starter templates, so a
    # person's targets live beside their data and the whole workspace stays
    # portable. Falls back to the repo copies only if this never runs.
    for name in ("job-sources.yaml", "content-sources.yaml"):
        dest = WS / "config" / name
        if not dest.exists():
            shutil.copy2(BASE / "config" / name, dest)
            created.append(f"config/{name}")

    # Identity belongs to the workspace; API keys stay machine-wide.
    ws_env = workspace.env_path(WS)
    if not ws_env.exists():
        template = BASE / "templates/workspace.env.example"
        if template.exists():
            shutil.copy2(template, ws_env)
            created.append(".env (workspace identity)")
    if not ENV_PATH.exists() and ENV_EXAMPLE.exists():
        shutil.copy2(ENV_EXAMPLE, ENV_PATH)
        created.append(".env (repo, API keys)")
    return created


# ---------- status ----------

def _is_placeholder(path):
    if not path.exists():
        return True
    text = path.read_text(errors="ignore")
    return any(marker in text for marker in PLACEHOLDER_MARKERS) and len(text) < 2000


def _env_file(key):
    """Identity belongs to the workspace, machine secrets to the repo."""
    return workspace.env_path(WS) if key in workspace.IDENTITY_KEYS else ENV_PATH


def _env_value(key):
    return workspace.read_env(_env_file(key), key)


def status():
    resume_ok = (ME / "resume.pdf").exists() or (
        (ME / "resume.txt").exists() and not _is_placeholder(ME / "resume.txt")
    )
    profile_ok = (ME / "profile.md").exists() and not _is_placeholder(ME / "profile.md")
    linkedin_ok = (ME / "linkedin" / "profile.pdf").exists() or \
        (ME / "linkedin" / "linkedin-export.zip").exists()

    backend = _env_value("LLM_BACKEND") or "cli"
    anthropic_key = bool(_env_value("ANTHROPIC_API_KEY"))
    openrouter_key = bool(_env_value("OPENROUTER_API_KEY"))
    github_user = _env_value("GITHUB_USERNAME")
    bluesky_handle = _env_value("BLUESKY_HANDLE")
    bluesky_pw = bool(_env_value("BLUESKY_APP_PASSWORD"))

    claude_cli = shutil.which("claude") is not None

    def line(label, ok, detail=""):
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label}{(' - ' + detail) if detail else ''}")

    print(f"workspace: {WS.name}  ({WS})")

    print("\nme/ (required for a first run)")
    line("resume", resume_ok)
    line("profile.md", profile_ok)

    print("\nme/ (optional)")
    line("linkedin", linkedin_ok, "skipped if absent")

    # Identity is per workspace: switching --user switches whose accounts get
    # scanned, so say out loud which file these came from.
    print(f"\nidentity ({workspace.env_path(WS)})")
    line("bluesky handle+password", bool(bluesky_handle and bluesky_pw), "skipped if absent")
    line("github username", bool(github_user), github_user or "skipped if absent")

    print(f"\nLLM backend ({ENV_PATH} - shared by every workspace)")
    if backend == "api":
        line("LLM_BACKEND=api", anthropic_key, "ANTHROPIC_API_KEY " + ("set" if anthropic_key else "MISSING"))
    else:
        line("LLM_BACKEND=cli", claude_cli, "claude CLI " + ("found" if claude_cli else "NOT FOUND - install it or set LLM_BACKEND=api"))
    line("OPENROUTER_API_KEY (optional, cheap-tier routing)", openrouter_key)

    ready = resume_ok and profile_ok and (
        (backend == "api" and anthropic_key) or (backend == "cli" and claude_cli)
    )
    print("\n" + ("Ready for `mc run`." if ready
                    else "Not ready yet - run `uv run setup.py` to finish."))
    return ready


# ---------- dry run ----------

def dry_run():
    sys.path.insert(0, str(BASE))
    import yaml
    companies = yaml.safe_load(workspace.config_path(WS, "job-sources.yaml").read_text()) or {}
    feeds = yaml.safe_load(workspace.config_path(WS, "content-sources.yaml").read_text()) or {}
    n_companies = len(companies.get("companies", []))
    n_feeds = len(feeds.get("sources", []))
    print(f"Would scan {n_companies} companies (config/job-sources.yaml)")
    print(f"Would scan {n_feeds} RSS feeds (config/content-sources.yaml)")
    print()
    status()
    print("\nNo LLM calls made. Run `mc run` for the real thing.")


# ---------- interactive wizard ----------

def wizard():
    created = scaffold()
    if created:
        print("Created:", ", ".join(created))
    else:
        print(f"{WS.name}: me/ and .env already exist - editing in place.")

    print()
    if _is_placeholder(ME / "profile.md"):
        print("me/profile.md needs your target roles and domain expertise.")
        if yes("Open a quick prompt to fill in the basics now?", default=True):
            roles = prompt("Top target role")
            domain = prompt("One-paragraph summary of what you do")
            if roles or domain:
                text = ME.joinpath("profile.md").read_text()
                if roles:
                    text = text.replace("1. <!-- e.g. Senior Data Scientist, Marketing Analytics -->",
                                         f"1. {roles}")
                if domain:
                    text += f"\n{domain}\n"
                ME.joinpath("profile.md").write_text(text)
        print(f"Edit further any time: {ME / 'profile.md'}")

    resume_pdf = ME / "resume.pdf"
    resume_txt = ME / "resume.txt"
    if not resume_pdf.exists() and _is_placeholder(resume_txt):
        path = prompt("Path to your resume (PDF or .txt), blank to skip for now")
        if path:
            src = Path(path).expanduser()
            if src.exists():
                dest = ME / ("resume.pdf" if src.suffix.lower() == ".pdf" else "resume.txt")
                shutil.copy2(src, dest)
                print(f"Copied to {dest}")
            else:
                print(f"Not found: {src} - skipping, drop it in {ME} manually later.")

    print()
    if yes("Configure BlueSky profile scanning? (optional)", default=False):
        handle = prompt("BlueSky handle (e.g. you.bsky.social)")
        pw = prompt("BlueSky app password (from bsky.app/settings/app-passwords)")
        if handle and pw:
            _set_env("BLUESKY_HANDLE", handle)
            _set_env("BLUESKY_APP_PASSWORD", pw)

    if yes("Configure GitHub profile scanning? (optional)", default=False):
        user = prompt("GitHub username")
        if user:
            _set_env("GITHUB_USERNAME", user)

    print()
    print("LinkedIn: drop a profile.pdf or linkedin-export.zip in "
          f"{ME / 'linkedin'} any time - see {ME / 'linkedin' / 'README.md'}.")

    print()
    ready = status()
    if ready and yes("\nRun the pipeline now?", default=True):
        import subprocess
        subprocess.run([sys.executable, str(BASE / "run_daily.py"), "--user", str(WS)],
                        cwd=str(BASE))


def _set_env(key, value):
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    workspace.add_argument(ap)
    ap.add_argument("--status", action="store_true", help="show what's configured, no prompts")
    ap.add_argument("--dry-run", action="store_true", help="preview scan scope, no LLM calls")
    args = ap.parse_args()

    # Rebind the module-level workspace paths before any command runs, so every
    # helper below writes into the workspace the user actually asked for.
    global WS, ME
    WS = workspace.resolve(args.user)
    ME = WS / "me"

    if args.status:
        ok = status()
        return 0 if ok else 1
    if args.dry_run:
        dry_run()
        return 0

    wizard()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Identity is per workspace: --user switches whose accounts a run would scan."""
import os, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

import workspace

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)


check("identity keys are named explicitly",
      set(workspace.IDENTITY_KEYS) == {"GITHUB_USERNAME", "BLUESKY_HANDLE",
                                        "BLUESKY_APP_PASSWORD", "BLUESKY_PDS_URL"},
      str(workspace.IDENTITY_KEYS))

# Two workspaces, two identities.
alice = Path(tempfile.mkdtemp())
bob = Path(tempfile.mkdtemp())
workspace.set_env(workspace.env_path(alice), "GITHUB_USERNAME", "alice-gh")
workspace.set_env(workspace.env_path(bob), "GITHUB_USERNAME", "bob-gh")

saved = dict(os.environ)
try:
    os.environ.pop("GITHUB_USERNAME", None)
    workspace.load_env(alice)
    check("workspace .env supplies identity", os.environ.get("GITHUB_USERNAME") == "alice-gh",
          os.environ.get("GITHUB_USERNAME", ""))

    # The second load must WIN, not leave the first person's value in place.
    workspace.load_env(bob)
    check("switching workspace switches identity", os.environ.get("GITHUB_USERNAME") == "bob-gh",
          os.environ.get("GITHUB_USERNAME", ""))

    # A workspace with no .env must not inherit whoever ran last from the repo .env.
    empty = Path(tempfile.mkdtemp())
    os.environ.pop("GITHUB_USERNAME", None)
    workspace.load_env(empty)
    check("a workspace with no .env has no identity (scan skips, never scans someone else)",
          not os.environ.get("GITHUB_USERNAME"), os.environ.get("GITHUB_USERNAME", ""))

    # Machine-wide keys still come from the repo .env for every workspace.
    repo_backend = workspace.read_env(REPO / ".env", "LLM_BACKEND")
    if repo_backend:
        workspace.load_env(alice)
        check("machine-wide keys reach every workspace",
              os.environ.get("LLM_BACKEND") == repo_backend, repo_backend)

    # The repo .env must no longer carry identity - that is the whole point.
    for key in workspace.IDENTITY_KEYS:
        check(f"repo .env does not define {key}", not workspace.read_env(REPO / ".env", key))
finally:
    os.environ.clear()
    os.environ.update(saved)

# The content radar must take the caller's user, not a hardcoded one.
import content_radar
src = Path(content_radar.__file__).read_text()
check("no hardcoded GitHub user in content_radar", 'GITHUB_USER = "' not in src)
radar = content_radar.ContentRadar(alice, github_user="")
check("empty github user means no GitHub call", radar.fetch_repos() == [])

if fails:
    print(f"\n{len(fails)} failed: {fails}")
    sys.exit(1)
print("\nall passed")

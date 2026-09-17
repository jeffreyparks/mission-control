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


# --- regression: the writer must route, not just the reader --------------
# This bug shipped once: _env_value was routed to the workspace file while
# _set_env still wrote every key to the repo .env, so running setup for one
# person put their identity in the file every workspace shares.
import setup as setup_mod

# The real repo .env is never the target here: if this test regresses, it must
# fail loudly, not quietly rewrite the developer's own secrets file.
target = Path(tempfile.mkdtemp())
stand_in_repo_env = Path(tempfile.mkdtemp()) / ".env"
stand_in_repo_env.write_text("OPENROUTER_API_KEY=machine-wide\n")

setup_mod.WS = target
setup_mod.ME = target / "me"
setup_mod.ENV_PATH = stand_in_repo_env

setup_mod._set_env("GITHUB_USERNAME", "written-by-setup")
check("setup writes identity into the workspace .env",
      workspace.read_env(workspace.env_path(target), "GITHUB_USERNAME") == "written-by-setup")
check("setup does NOT put identity in the shared .env",
      not workspace.read_env(stand_in_repo_env, "GITHUB_USERNAME"),
      stand_in_repo_env.read_text().strip())

# A machine-wide key still belongs to the shared file.
setup_mod._set_env("OPENROUTER_API_KEY", "still-machine-wide")
check("machine keys are written to the shared .env",
      workspace.read_env(stand_in_repo_env, "OPENROUTER_API_KEY") == "still-machine-wide")
check("machine keys never land in the workspace .env",
      not workspace.read_env(workspace.env_path(target), "OPENROUTER_API_KEY"))
check("machine keys route to the shared .env", setup_mod._env_file("OPENROUTER_API_KEY") == stand_in_repo_env)
check("identity keys route to the workspace .env",
      setup_mod._env_file("GITHUB_USERNAME") == workspace.env_path(target))

# The developer's real repo .env must carry no identity either.
check("the real repo .env defines no identity",
      not any(workspace.read_env(REPO / ".env", k) for k in workspace.IDENTITY_KEYS))

# --- structural: a shared-file identity can never be inherited -----------
# The earlier version of this test only passed because the repo .env happened
# to be clean. Plant a value and prove a bare workspace still gets nothing.
saved2 = dict(os.environ)
try:
    os.environ["GITHUB_USERNAME"] = "someone-elses-account"
    os.environ["BLUESKY_HANDLE"] = "someone-elses-handle"
    bare = Path(tempfile.mkdtemp())          # no .env at all
    workspace.load_env(bare)
    check("a bare workspace inherits NO identity, even when one is in the ambient env",
          not os.environ.get("GITHUB_USERNAME") and not os.environ.get("BLUESKY_HANDLE"),
          f"github={os.environ.get('GITHUB_USERNAME', '')!r}")

    # ...and a workspace WITH a file still gets its own values.
    mine = Path(tempfile.mkdtemp())
    workspace.set_env(workspace.env_path(mine), "GITHUB_USERNAME", "mine")
    os.environ["GITHUB_USERNAME"] = "someone-elses-account"
    workspace.load_env(mine)
    check("a workspace with a file gets its own identity", os.environ.get("GITHUB_USERNAME") == "mine",
          os.environ.get("GITHUB_USERNAME", ""))
finally:
    os.environ.clear()
    os.environ.update(saved2)

if fails:
    print(f"\n{len(fails)} failed: {fails}")
    sys.exit(1)
print("\nall passed")

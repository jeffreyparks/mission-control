"""max_live_roles is config-driven (rules.max_live_roles), with explicit
overrides (CLI --max-live, or add_role.py's 0) always winning."""
import sys, tempfile, yaml
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

from job_intel import JobIntel, DEFAULT_MAX_LIVE_ROLES, load_max_live_roles
from job_scanner import JobScanner

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

def make_ws(rules=None):
    ws = Path(tempfile.mkdtemp())
    (ws / "config").mkdir(parents=True)
    doc = {"companies": [], "aggregators": [], "linkedin": {"enabled": False, "search_keywords": []},
           "rules": rules or {}}
    (ws / "config/job-sources.yaml").write_text(yaml.safe_dump(doc))
    return ws

# No config value -> the documented default, not silently 0 or None.
bare = make_ws()
check("no rules.max_live_roles falls back to the documented default",
      load_max_live_roles(JobScanner(bare)) == DEFAULT_MAX_LIVE_ROLES, str(DEFAULT_MAX_LIVE_ROLES))
check("JobIntel with no explicit value picks up the same default",
      JobIntel(bare).max_live_roles == DEFAULT_MAX_LIVE_ROLES)

# A workspace's own config value is honored.
custom = make_ws({"max_live_roles": 42})
check("a configured value is used", load_max_live_roles(JobScanner(custom)) == 42)
check("JobIntel(base_dir) alone reads the workspace's config", JobIntel(custom).max_live_roles == 42)

# An explicit constructor value - CLI --max-live - always wins over config.
check("an explicit value overrides the config", JobIntel(custom, max_live_roles=7).max_live_roles == 7)
check("an explicit ZERO overrides the config too (add_role.py's --max-live 0)",
      JobIntel(custom, max_live_roles=0).max_live_roles == 0)

# A bad value in the file must degrade to the default, never crash a run.
bad = make_ws({"max_live_roles": "lots"})
check("a non-numeric config value falls back to the default, not a crash",
      load_max_live_roles(JobScanner(bad)) == DEFAULT_MAX_LIVE_ROLES)

# The real project template documents the key so a new workspace has it.
tpl = yaml.safe_load((REPO / "config/job-sources.yaml").read_text())
check("the starter template documents max_live_roles", "max_live_roles" in tpl.get("rules", {}),
      str(tpl.get("rules", {}).get("max_live_roles")))

if fails:
    print(f"\n{len(fails)} failed: {fails}")
    sys.exit(1)
print("\nall passed")

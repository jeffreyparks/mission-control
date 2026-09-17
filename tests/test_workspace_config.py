"""Per-workspace config: a person's targets live with their data, not in the repo."""
import sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

import yaml
import workspace
from job_scanner import JobScanner
from content_radar import ContentRadar

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

CONFIGS = ("job-sources.yaml", "content-sources.yaml")

# The repo copies are a starter template: they must stay valid, because a
# workspace with no config of its own falls back to them.
for name in CONFIGS:
    doc = yaml.safe_load((REPO / "config" / name).read_text())
    check(f"repo template {name} parses", isinstance(doc, dict))
tpl = yaml.safe_load((REPO / "config/job-sources.yaml").read_text())
check("template keeps the full schema",
      {"companies", "aggregators", "linkedin", "rules"} <= set(tpl),
      str(sorted(tpl)))
check("template rules keep the keys the scanner reads",
      {"min_match_score", "must_have_keywords", "auto_close_titles"} <= set(tpl["rules"]),
      str(sorted(tpl["rules"])))

# A workspace with its own config must win over the repo template.
ws = Path(tempfile.mkdtemp())
(ws / "config").mkdir(parents=True)
(ws / "config/job-sources.yaml").write_text(
    yaml.safe_dump({"companies": [{"name": "Only Mine", "careers_url": "https://x", "api_url": None,
                                    "priority": 2}],
                    "aggregators": [], "linkedin": {"enabled": False, "search_keywords": []},
                    "rules": {"min_match_score": 50, "must_have_keywords": [],
                              "exclude_keywords": [], "max_age_days": 30,
                              "auto_close_titles": []}}))
picked = JobScanner(ws).sources_path
check("workspace config wins over the repo template", picked == ws / "config/job-sources.yaml", str(picked))

# ...and a workspace without one still works, via the template.
bare = Path(tempfile.mkdtemp())
check("a bare workspace falls back to the repo template",
      JobScanner(bare).sources_path == REPO / "config/job-sources.yaml")
check("content radar falls back the same way",
      ContentRadar(bare).sources_path == REPO / "config/content-sources.yaml")

# setup.py seeds a new workspace so it is self-contained and portable.
import setup as setup_mod
fresh = Path(tempfile.mkdtemp())
setup_mod.WS = fresh
setup_mod.ME = fresh / "me"
created = setup_mod.scaffold()
for name in CONFIGS:
    check(f"scaffold seeds {name} into the workspace", (fresh / "config" / name).exists())
check("scaffold seeds the workspace identity .env", workspace.env_path(fresh).exists(), str(created))
check("seeded config is the workspace's own, not the repo's",
      JobScanner(fresh).sources_path == fresh / "config/job-sources.yaml")

if fails:
    print(f"\n{len(fails)} failed: {fails}")
    sys.exit(1)
print("\nall passed")

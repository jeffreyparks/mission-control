"""Content Radar sizes its own output budgets instead of taking the 4096 default.

At 4096 the article answer (up to TARGET_PICKS picks of several sentences each,
plus filter and network sections) was cut off mid-JSON, so the run lost every
pick. The repo call's budget scales with the repo count, within the ceiling.
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import llm as llm_mod
from content_radar import ContentRadar

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)


class _RecordingLLM:
    def __init__(self): self.calls = {}
    def complete_json(self, prompt, tag=None, max_tokens=None, **kw):
        self.calls[tag] = max_tokens
        return {"picks": [], "repos": []}


radar = ContentRadar.__new__(ContentRadar)   # skip __init__: no workspace, no LLM
radar.base_dir = Path(tempfile.mkdtemp())    # empty me/: context_block tolerates it
radar.watch_topics = []
radar.github_user = "u"
radar.llm = _RecordingLLM()

article = {"source": "S", "category": "C", "title": "T", "url": "https://example.com",
           "published": __import__("datetime").datetime(2026, 9, 20), "text": "body " * 100}
radar.analyze_articles([article], "2026-09-26")
budget = radar.llm.calls.get("content-radar-articles")
check("article call passes a budget", budget is not None, str(budget))
check("article budget is well above the old 4096", budget and budget >= 9000, str(budget))
check("article budget is within the ceiling", budget and budget <= llm_mod.MAX_TOKENS_CEILING)


def repo_budget(n):
    repos = [{"name": f"r{i}", "url": f"https://github.com/u/r{i}", "language": "Python",
              "stars": 0, "pushed_at": "2026-09-01", "topics": [], "archived": False,
              "description": ""} for i in range(n)]
    radar.analyze_repos(repos, [], "", "2026-09-26")
    return radar.llm.calls.get("content-radar-repos")

small, medium, large = repo_budget(3), repo_budget(40), repo_budget(100)
check("3 repos get the default floor", small == llm_mod.DEFAULT_MAX_TOKENS, str(small))
check("repo budget grows with the repo count", medium > small, f"{small} -> {medium}")
check("100 repos stay within the ceiling", large <= llm_mod.MAX_TOKENS_CEILING, str(large))

if fails:
    print(f"\n{len(fails)} failed")
    sys.exit(1)
print("\nall passed")

"""Content Radar targets a generous pick count - and nothing truncates it.

The pick target is a module constant (TARGET_PICKS) rather than a number
typed into the prompt, so raising it can never drift out of sync with the
text the model actually reads.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

src = (REPO / "agents" / "content_radar.py").read_text()

import content_radar as cr_mod

check("the pick target is a constant, not a magic number in the prompt",
      cr_mod.TARGET_PICKS >= 6 and "{TARGET_PICKS} picks is the target" in src,
      f"TARGET_PICKS={cr_mod.TARGET_PICKS}")
check("the old hardcoded 3-6 range is gone",
      "3 to 6 picks is the target" not in src and "6 picks is the target" not in src)
check("breadth is enforced in code, not only in the prompt",
      cr_mod.MAX_PICKS_PER_THEME >= 1 and "MAX_PICKS_PER_THEME" in src)
check("the honesty escape valve (zero is fine) is still present",
      "ZERO picks is a" in src or "zero picks" in src.lower())
check("fewer-than-padded guidance is still present", "fewer is still better than padded" in src
      or "fewer is better than padded" in src)
check("nothing truncates the model's own picks list to a fixed count before output",
      not re.search(r'picks\s*\[\s*:\s*[0-9]+\s*\]', src))

# ---------------- a bare JSON array must not kill the run ----------------
# LLM._extract_json is documented to return "the first JSON object OR ARRAY",
# so a model answering with a bare [...] of picks instead of the requested
# {"picks": [...]} wrapper is a SUCCESSFUL parse. That shape used to raise
# "'list' object has no attribute 'get'" and, because the parse succeeded, it
# was cached - so the crash repeated on every later run until the cache entry
# was removed by hand.
import datetime

radar = cr_mod.ContentRadar(REPO / "workspace" / "default")
arts = [{"url": "u0", "source": "S", "category": "C", "title": "T",
         "published": datetime.datetime.now(), "text": "body"}]
one_pick = {"tier": "primary", "theme": "Agentic Systems", "headline": "h",
            "source_url": "u0", "why_it_matters": "w", "angle": "a",
            "formats": ["Tweet"], "source_name": "S", "source_date": "2026-09-21"}

radar.llm.complete_json = lambda prompt, tag=None, validate=None: [one_pick]
res = radar.analyze_articles(arts, "2026-09-21")
check("a bare array of picks is coerced to the documented wrapper",
      isinstance(res, dict) and len(res.get("picks") or []) == 1, str(type(res)))

radar.llm.complete_json = lambda prompt, tag=None, validate=None: "not json at all"
check("an unusable scalar degrades to empty, it does not raise",
      radar.analyze_articles(arts, "2026-09-21") == {})

radar.llm.complete_json = lambda prompt, tag=None, validate=None: [{"name": "r", "verdict": "no-change"}]
check("a bare array of repos is coerced too",
      (radar.analyze_repos([{"name": "r", "url": "u", "language": "", "stars": 0,
                             "pushed_at": "", "topics": [], "archived": False,
                             "description": ""}], [], "f", "2026-09-21")
       or {}).get("repos") is not None)

check("the wrapper is also demanded up front via validate=",
      "validate=self._wants_object" in src and src.count("validate=self._wants_object") == 2)
check("_wants_object rejects a list and accepts a dict",
      radar._wants_object({"picks": []}) is True and radar._wants_object([]) is False)

stats = {"sources_configured": 1, "sources_with_entries": 1,
         "articles_considered": 1, "articles_fetched": 1}
check("build_payload survives None results",
      radar.build_payload("2026-09-21", arts, None, None, [], stats)["stats"]["picks"] == 0)
check("build_payload survives raw list results",
      radar.build_payload("2026-09-21", arts, [one_pick], [], [], stats)["stats"]["picks"] == 1)

# render_radar (the full weekly page) must show every pick, not a subset -
# only the HOME PAGE teaser is deliberately capped at 3.
build_src = (REPO / "render" / "build.py").read_text()
radar_fn = build_src[build_src.find("def render_radar"):build_src.find("def render_radar") + 400]
check("the full radar page renders every pick, unsliced",
      "picks=_radar_picks(d)" in radar_fn and "[:3]" not in radar_fn)
check("only the home-page teaser stays capped at 3 (a different, deliberate design choice)",
      "top_picks=_radar_picks(radar)[:3]" in build_src)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

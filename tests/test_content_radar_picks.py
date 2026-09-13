"""Content Radar targets 6 picks, not 3 - and nothing truncates the result."""
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

check("prompt targets 6 picks, not the old 3-6 range",
      "6 picks is the target" in src and "3 to 6 picks is the target" not in src)
check("the honesty escape valve (zero is fine) is still present",
      "ZERO picks is a" in src or "zero picks" in src.lower())
check("fewer-than-padded guidance is still present", "fewer is still better than padded" in src
      or "fewer is better than padded" in src)
check("nothing truncates the model's own picks list to a fixed count before output",
      not re.search(r'picks\s*\[\s*:\s*[0-9]+\s*\]', src))

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

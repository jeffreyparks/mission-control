"""Rendered pages: outbound links open in a new context; nav links do not."""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "render"))

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

templates = {
    "tracker.html.j2": ['{{ r.url }}', '{{ o.best_role.url }}'],
    "index.html.j2": ['{{ r.url }}'],
    "radar.html.j2": ['{{ p.source_url }}'],
}
for name, urls in templates.items():
    src = (REPO / "render" / name).read_text()
    for token in urls:
        needle = f'href="{token}"'
        pos = src.find(needle)
        check(f"{name}: {token} has target=new + rel=noopener", pos != -1 and
              'target="new" rel="noopener"' in src[pos:pos + len(needle) + 40],
              "not found" if pos == -1 else src[pos:pos+60])

nav = (REPO / "render" / "_nav.html.j2").read_text()
check("internal nav links do NOT get target=new", 'target="new"' not in nav)

idx_src = (REPO / "render" / "index.html.j2").read_text()
check("the internal 'Open the radar' link does NOT get target=new",
      'href="content-radar.html"' in idx_src and
      'target="new"' not in idx_src[idx_src.find('href="content-radar.html"'):idx_src.find('href="content-radar.html"') + 60])

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

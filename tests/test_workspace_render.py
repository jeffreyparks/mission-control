"""Tests for render/build.py --user: republish one workspace, leaving others alone."""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

work = Path(tempfile.mkdtemp())
(work / "artifacts/jobs").mkdir(parents=True)
(work / "artifacts/content").mkdir(parents=True)

intel_fixture = {
    "generated_at": "2026-01-01T00:00:00", "date": "2026-01-01",
    "counts": {"apply": 1, "research": 0, "skip": 0}, "llm": "LLM: 0 calls, 0 cached, $0.000",
    "roles": [{
        "id": "acme-1", "store_id": None, "source": "tracker", "org": "Acme",
        "title": "Head of Measurement", "url": None, "location": None, "role_cat": None,
        "priority": 1, "status": "01 Open", "date_opened": None, "date_applied": None,
        "outcome": None, "outcome_label": None, "days_to_outcome": None, "outcome_display": None,
        "notes": None, "comp_range": None, "fit_score": 80, "seniority_read": "x", "why": "x",
        "top_gaps": [], "what_theyre_really_hiring_for": "x", "pitch_angle": "x",
        "recommendation": "apply", "suggested_role_cat": None, "suggestion_confidence": None,
        "suggestion_reason": None, "sector": "Tech - SaaS",
    }],
    "orgs": [],
}
(work / "artifacts/jobs" / "intel-2026-01-01.json").write_text(json.dumps(intel_fixture))

result = subprocess.run(
    [sys.executable, str(REPO / "render/build.py"), "--user", str(work)],
    cwd=str(REPO), capture_output=True, text=True,
)
check("build.py --user exits 0", result.returncode == 0, result.stderr[-500:])

alt_html = work / "artifacts/html"
check("tracker rendered into the alt workspace", (alt_html / "job-tracker.html").exists())
check("index rendered into the alt workspace", (alt_html / "index.html").exists())
check("theme.css copied into the alt workspace", (alt_html / "theme.css").exists())

# The default workspace must be untouched by a render aimed at another one.
default_index = REPO / "workspace/default/artifacts/html/index.html"
before = default_index.read_text() if default_index.exists() else None
check("the default workspace is not touched",
      before is None or "Acme" not in before)

if (alt_html / "job-tracker.html").exists():
    body = (alt_html / "job-tracker.html").read_text()
    check("tracker page contains the alt fixture's role", "Head of Measurement" in body)

# Theme switch: every page ships the button, and the saved choice is applied
# from <head> - after the stylesheet link but before any content - so the page
# never paints in the wrong theme first.
# content-radar.html is only rendered when there is radar data, which this
# fixture does not provide - checked when present, not demanded.
for name in ("index.html", "content-radar.html", "job-tracker.html"):
    page = alt_html / name
    if not page.exists():
        continue
    text = page.read_text()
    check(f"{name} ships the theme switch", 'id="themebtn"' in text)
    check(f"{name} applies the saved theme before paint",
          text.index("mc-theme") < text.index("<body"))

css = (alt_html / "theme.css").read_text()
check("theme.css carries an explicit light palette", '[data-theme="light"]' in css)
check("theme.css falls back to the system preference",
      "prefers-color-scheme:light" in css.replace(" ", ""))
check("no raw hex colours leak past the palette block",
      "#" not in css.split("*{box-sizing:border-box}", 1)[1])

shutil.rmtree(work, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} failed: {fails}")
    sys.exit(1)
print("\nall passed")

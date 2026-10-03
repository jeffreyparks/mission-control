"""`run_daily.py --radar-only` runs the content radar and the page rebuild - nothing else.

It must skip the job scan, profiles, and run the radar even when
the weekly cadence says it is not due.
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import run_daily

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

ran = []
for name in ("profiles", "jobs", "radar", "render"):
    setattr(run_daily, f"run_{name}", lambda n=name: ran.append(n))
run_daily.radar_is_due = lambda: (False, "3d until next radar")
run_daily.log_routing = lambda: None
work = Path(tempfile.mkdtemp())
run_daily.workspace.resolve = lambda user=None: work
run_daily.workspace.load_env = lambda ws: None


def main(*argv):
    ran.clear()
    sys.argv = ["run_daily.py", *argv]
    try:
        return run_daily.main()
    except SystemExit as exc:
        return exc.code


rc = main("--radar-only")
check("--radar-only exits 0", rc == 0, str(rc))
check("--radar-only runs radar then render only", ran == ["radar", "render"], str(ran))

rc = main("--radar-only", "--only", "jobs")
check("--radar-only with --only is rejected", rc not in (0, None) and ran == [], f"{rc} {ran}")

rc = main()
check("a normal run still skips a radar that is not due", "radar" not in ran and "jobs" in ran,
      str(ran))

rc = main("--profile-only")
check("--profile-only exits 0", rc == 0, str(rc))
check("--profile-only runs profiles then render only", ran == ["profiles", "render"], str(ran))

rc = main("--profile-only", "--radar-only")
check("--profile-only with --radar-only is rejected", rc not in (0, None) and ran == [],
      f"{rc} {ran}")

if fails:
    print(f"\n{len(fails)} failed")
    sys.exit(1)
print("\nall passed")

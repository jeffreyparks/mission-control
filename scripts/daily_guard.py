#!/usr/bin/env python3
"""
Daily-run guard for Mission Control.

Called by the Prime Agent heartbeat. Decides whether today's pipeline run is
due, and runs it if so. Safe to call many times a day: it runs at most once.

Prints exactly one of:
  SKIP <reason>
  RAN  <summary lines...>
  FAIL <reason>

Exit code is 0 for SKIP and RAN, 1 for FAIL.
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STATE = BASE / "data/last-daily-run.txt"
EARLIEST_HOUR = 7          # do not run before 07:00 local
TIMEOUT_S = 1800           # 30 minutes


def main():
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    if now.hour < EARLIEST_HOUR:
        print(f"SKIP before {EARLIEST_HOUR:02d}:00 local (now {now:%H:%M})")
        return 0

    last = STATE.read_text().strip() if STATE.exists() else ""
    if last == today:
        print(f"SKIP already ran today ({today})")
        return 0

    proc = subprocess.run(
        ["uv", "run", "run_daily.py"],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )
    log_dir = BASE / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / f"daily-{today}.log").write_text(proc.stdout + proc.stderr)

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        print(f"FAIL run_daily.py exited {proc.returncode}")
        print("\n".join(tail))
        return 1

    # Only mark the day done on success, so a failure retries on the next beat.
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(today)

    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    print(f"RAN  {today}")
    print("\n".join(lines[-8:]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.TimeoutExpired:
        print(f"FAIL run_daily.py timed out after {TIMEOUT_S}s")
        sys.exit(1)

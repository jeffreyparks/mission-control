"""Run the script-style tests under pytest.

Each tests/test_*.py here is a standalone script: it runs its checks at module
level, prints `ok`/`FAIL` per check and exits non-zero if any failed. Importing
one as a pytest module would run it at import time and hit its sys.exit, so
instead each script is collected as a single test that runs it in its own
interpreter - the same isolation `uv run python tests/test_X.py` gives, since
several scripts patch module globals and os.environ.

A script that ends by printing `SKIP: ...` (e.g. a live network check with no
network) is reported as skipped. A file that defines its own top-level
`def test_*` functions is left to pytest's normal collection, so new tests can
be written either way.
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 300


def _is_script(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    return not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        for node in tree.body
    )


def pytest_pycollect_makemodule(module_path, parent):
    if _is_script(module_path):
        return ScriptFile.from_parent(parent, path=module_path)
    return None


class ScriptFile(pytest.File):
    def collect(self):
        yield ScriptItem.from_parent(self, name=self.path.stem)


class ScriptFailed(Exception):
    pass


class ScriptItem(pytest.Item):
    def runtest(self):
        try:
            proc = subprocess.run(
                [sys.executable, str(self.path)],
                cwd=REPO, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise ScriptFailed(f"timed out after {TIMEOUT_SECONDS}s")
        self.output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            raise ScriptFailed(f"exit code {proc.returncode}")
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        if lines and lines[-1].startswith("SKIP:"):
            pytest.skip(lines[-1][len("SKIP:"):].strip())

    def repr_failure(self, excinfo):
        if not isinstance(excinfo.value, ScriptFailed):
            return super().repr_failure(excinfo)
        output = getattr(self, "output", "")
        failed = [l for l in output.splitlines() if l.startswith("FAIL ")]
        parts = [f"{self.path.name}: {excinfo.value}"]
        if failed:
            parts += ["", "failed checks:"] + [f"  {l}" for l in failed]
        parts += ["", "output:", output.rstrip()]
        return "\n".join(parts)

    def reportinfo(self):
        return self.path, None, f"script {self.path.name}"

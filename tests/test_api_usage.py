"""LLM usage is reported as calls and exact token counts - never dollars.

Costs were dropped: the Anthropic API returns no price, and a hand-kept rate
table went stale (an unlisted model silently counted as nothing). Tokens are
measured, not estimated, so they stay.
"""
import os, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

import routing
import llm as llm_mod
from llm import LLM

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

check("no price table loader remains", not hasattr(routing, "load_prices"))
check("no cost function remains", not hasattr(llm_mod, "api_cost"))
check("no price table in config", "[prices]" not in (REPO / "config/model-routing.toml").read_text())


class _Resp:
    status_code = 200
    def json(self):
        return {"content": [{"type": "text", "text": '{"ok": true}'}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 2000,
                          "cache_creation_input_tokens": 4000,
                          "cache_read_input_tokens": 300_000}}

saved = dict(os.environ)
real_post = llm_mod.requests.post
try:
    os.environ["LLM_BACKEND"] = "api"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    llm_mod.requests.post = lambda *a, **k: _Resp()

    client = LLM(Path(tempfile.mkdtemp()), route=False)
    check("stats carry no cost field", not any("cost" in k for k in client.stats), str(list(client.stats)))
    client._call_anthropic_api("x", "claude-sonnet-5")
    check("input tokens are counted", client.stats["api_input_tokens"] == 1000)
    check("output tokens are counted", client.stats["api_output_tokens"] == 2000)
    check("cache tokens are counted",
          client.stats["cache_read_tokens"] == 300_000 and client.stats["cache_write_tokens"] == 4000)

    report = client.report()
    check("the report shows API token counts", "api 1,000 in / 2,000 out" in report, report)
    check("the report shows the cache split", "300,000 read / 4,000 written" in report, report)
    check("the report has no dollar figure", "$" not in report, report)
    check("the report has no pricing hint", "price" not in report.lower(), report)
finally:
    llm_mod.requests.post = real_post
    os.environ.clear(); os.environ.update(saved)

if fails:
    print(f"\n{len(fails)} failed: {fails}")
    sys.exit(1)
print("\nall passed")

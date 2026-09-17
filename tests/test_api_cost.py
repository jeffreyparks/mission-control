"""API calls are priced from the table; the Anthropic API reports tokens, not cost."""
import os, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

import routing
import llm as llm_mod
from llm import LLM, api_cost

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

USAGE = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

prices = routing.load_prices()
check("the price table is loaded from config", bool(prices), str(sorted(prices)[:3]))
check("the routed models are priced",
      {"claude-sonnet-5", "claude-opus-5"} <= set(prices), str(sorted(prices)))

# A million in and a million out must equal the table's own rates.
p = prices["claude-sonnet-5"]
check("cost is the table's rate, not a guess",
      abs(api_cost("claude-sonnet-5", USAGE) - (p["input"] + p["output"])) < 1e-9,
      str(api_cost("claude-sonnet-5", USAGE)))

# Cached reads must be far cheaper than fresh input - that is the whole point.
cached = {"input_tokens": 0, "output_tokens": 0,
          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1_000_000}
fresh = {"input_tokens": 1_000_000, "output_tokens": 0,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
check("a cached read costs a fraction of fresh input",
      api_cost("claude-sonnet-5", cached) < api_cost("claude-sonnet-5", fresh) / 5,
      f"{api_cost('claude-sonnet-5', cached)} vs {api_cost('claude-sonnet-5', fresh)}")
check("opus costs more than sonnet for identical usage",
      api_cost("claude-opus-5", USAGE) > api_cost("claude-sonnet-5", USAGE))

# An unpriced model is unknown spend, never free.
check("an unpriced model returns None, not 0.0", api_cost("no-such-model", USAGE) is None)

# ---- the call path accumulates cost and tokens --------------------------
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
    client._call_anthropic_api("x", "claude-sonnet-5")
    expected = api_cost("claude-sonnet-5", _Resp().json()["usage"])
    check("the call adds its cost to the running total",
          abs(client.stats["cost_usd"] - expected) < 1e-9,
          f"{client.stats['cost_usd']:.6f} vs {expected:.6f}")
    check("a run that uses the API no longer reports $0.000", client.stats["cost_usd"] > 0)
    check("input tokens are counted", client.stats["api_input_tokens"] == 1000)
    check("output tokens are counted", client.stats["api_output_tokens"] == 2000)
    check("cache tokens are counted",
          client.stats["cache_read_tokens"] == 300_000 and client.stats["cache_write_tokens"] == 4000)

    report = client.report()
    check("the report shows API token counts", "api 1,000 in / 2,000 out" in report, report)
    check("the report shows the cache split", "300,000 read" in report, report)
    check("the report shows a non-zero cost", "$0.000" not in report, report)

    # An unpriced model must be called out, not silently costed at zero.
    client2 = LLM(Path(tempfile.mkdtemp()), route=False)
    client2._call_anthropic_api("x", "mystery-model-9")
    check("an unpriced model is flagged in the report",
          "unpriced" in client2.report() and "mystery-model-9" in client2.report(), client2.report())
    check("an unpriced model does not inflate cost", client2.stats["cost_usd"] == 0.0)
finally:
    llm_mod.requests.post = real_post
    os.environ.clear(); os.environ.update(saved)

if fails:
    print(f"\n{len(fails)} failed: {fails}")
    sys.exit(1)
print("\nall passed")

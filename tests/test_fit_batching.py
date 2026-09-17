"""Fit batching: output budget, truncation handling, prompt caching, batch splitting."""
import json, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

import yaml
import llm as llm_mod
from llm import LLM, LLMError, TruncatedResponse, output_budget, DEFAULT_MAX_TOKENS
from job_intel import JobIntel, DEFAULT_FIT_BATCH_SIZE, load_fit_batch_size
from job_scanner import JobScanner

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

# ---- output budget scales with the request -------------------------------
check("budget grows with the batch", output_budget(6) > output_budget(1), f"{output_budget(1)} -> {output_budget(6)}")
check("a 16-role batch is allowed more than the old fixed 4096",
      output_budget(16) > 4096, str(output_budget(16)))
check("budget never drops below the default floor", output_budget(1) >= DEFAULT_MAX_TOKENS)
check("budget is capped", output_budget(10_000) <= llm_mod.MAX_TOKENS_CEILING, str(output_budget(10_000)))
# 6 roles at the measured p90 (307 tok) plus headroom must fit comfortably
check("a batch of 6 has real headroom over its p90 need", output_budget(6) > 6 * 307 * 1.3,
      f"{output_budget(6)} vs {int(6 * 307 * 1.3)}")

# ---- truncation is reported, not mistaken for bad JSON -------------------
class _Resp:
    status_code = 200
    def __init__(self, stop): self._stop = stop
    def json(self):
        return {"content": [{"type": "text", "text": '[{"id": "x"'}],
                "stop_reason": self._stop,
                "usage": {"input_tokens": 1, "output_tokens": 4096}}

sent = {}
def _post_truncated(url, headers=None, json=None, timeout=None):
    sent.update(json or {})
    return _Resp("max_tokens")

import os
saved = dict(os.environ)
real_post = llm_mod.requests.post
try:
    os.environ["LLM_BACKEND"] = "api"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    llm_mod.requests.post = _post_truncated
    client = LLM(Path(tempfile.mkdtemp()), route=False)
    try:
        client._call_anthropic_api("x", "claude-sonnet-5", max_tokens=4096)
        check("a truncated response raises", False)
    except TruncatedResponse as exc:
        check("a truncated response raises TruncatedResponse", True)
        check("the error names the budget, not a JSON problem",
              "budget" in str(exc) and "cut off" in str(exc), str(exc)[:80])

    # Truncation must abort the ladder: retrying or escalating hits the same ceiling.
    calls = []
    def _count_post(url, headers=None, json=None, timeout=None):
        calls.append((json or {}).get("model"))
        return _Resp("max_tokens")
    llm_mod.requests.post = _count_post
    client2 = LLM(Path(tempfile.mkdtemp()), route=False)
    try:
        client2.complete_json("x", tag="job-fit-6", force=True)
    except TruncatedResponse:
        pass
    check("truncation is not retried or escalated (one call, not four)", len(calls) == 1, str(calls))

    # ---- prompt caching -------------------------------------------------
    llm_mod.requests.post = lambda url, headers=None, json=None, timeout=None: (
        sent.update(json or {}) or _Resp("end_turn"))
    client3 = LLM(Path(tempfile.mkdtemp()), route=False)
    big_prefix = "stable context. " * 400
    client3._call_anthropic_api("the variable part", "claude-sonnet-5", cache_prefix=big_prefix)
    # The caller's budget must reach the wire - testing output_budget() alone
    # would pass even if _call_anthropic_api ignored it and sent a fixed 4096.
    sent.clear()
    client3._call_anthropic_api("v", "claude-sonnet-5", max_tokens=9999)
    check("the caller's max_tokens is what gets sent", sent.get("max_tokens") == 9999, str(sent.get("max_tokens")))
    sent.clear()
    client3._call_anthropic_api("v", "claude-sonnet-5")
    check("no budget given falls back to the default floor",
          sent.get("max_tokens") == DEFAULT_MAX_TOKENS, str(sent.get("max_tokens")))
    sent.clear()
    client3._call_anthropic_api("the variable part", "claude-sonnet-5", cache_prefix=big_prefix)
    check("the prefix is sent as a system block", bool(sent.get("system")), str(type(sent.get("system"))))
    check("the prefix is marked for caching",
          sent["system"][0].get("cache_control", {}).get("type") == "ephemeral", str(sent["system"][0].keys()))
    check("only the variable part is in the user message",
          sent["messages"][0]["content"] == "the variable part", sent["messages"][0]["content"][:40])
    check("the prefix is not duplicated into the message",
          "stable context." not in sent["messages"][0]["content"])

    # A prefix too small to cache must still be sent, just uncached.
    sent.clear()
    client3._call_anthropic_api("v", "claude-sonnet-5", cache_prefix="too small")
    check("a tiny prefix is still sent, without a cache marker",
          sent["system"][0]["text"] == "too small" and "cache_control" not in sent["system"][0])
finally:
    llm_mod.requests.post = real_post
    os.environ.clear(); os.environ.update(saved)

# ---- splitting a prompt must not change its cache key -------------------
c = LLM(Path(tempfile.mkdtemp()), route=False)
check("cache key covers prefix and prompt together",
      c._key("PREFIX\n\nBODY", "t") == c._key(llm_mod._join_prompt("PREFIX", "BODY"), "t"))

# ---- batch size is config-driven ----------------------------------------
def make_ws(rules):
    ws = Path(tempfile.mkdtemp()); (ws / "config").mkdir(parents=True)
    (ws / "config/job-sources.yaml").write_text(yaml.safe_dump(
        {"companies": [], "aggregators": [], "linkedin": {"enabled": False, "search_keywords": []},
         "rules": rules}))
    return ws

check("default batch size is the measured-good small batch", DEFAULT_FIT_BATCH_SIZE == 6, str(DEFAULT_FIT_BATCH_SIZE))
check("no config value falls back to the default",
      load_fit_batch_size(JobScanner(make_ws({}))) == DEFAULT_FIT_BATCH_SIZE)
check("a configured batch size is used", load_fit_batch_size(JobScanner(make_ws({"fit_batch_size": 3}))) == 3)
check("a nonsense batch size falls back", load_fit_batch_size(JobScanner(make_ws({"fit_batch_size": "big"}))) == 6)
check("an explicit batch_size overrides config", JobIntel(make_ws({"fit_batch_size": 3}), batch_size=9).batch_size == 9)
check("JobIntel reads batch size from its workspace", JobIntel(make_ws({"fit_batch_size": 4})).batch_size == 4)

# ---- a failing batch is split, not lost ---------------------------------
ws = make_ws({"fit_batch_size": 6})
intel = JobIntel(ws, max_live_roles=0)
batch = [{"id": f"r{i}", "org": "O", "title": "T", "url": None, "location": None,
          "jd": "measurement", "comp_range": None} for i in range(6)]

class _StubLLM:
    """Fails for any batch larger than 2, succeeds below that - so the split
    has to happen for every role to come back judged."""
    def __init__(self): self.sizes = []
    def complete_json(self, prompt, tag=None, validate=None, cache_prefix=None, max_tokens=None, force=False):
        n = prompt.count("[r")  or prompt.count("r")  # rough count of roles in the block
        size = int(tag.rsplit("-", 1)[-1])
        self.sizes.append(size)
        if size > 2:
            raise LLMError("stubbed failure for a large batch")
        return [{"id": f"r{i}", "fit_score": 10, "seniority_read": "x", "why": "x",
                 "top_gaps": [], "what_theyre_really_hiring_for": "x",
                 "pitch_angle": None, "recommendation": "skip"} for i in range(6)]

intel.llm = _StubLLM()
results = {}
intel._judge_batch(batch, results, label="t")
check("splitting recovers every role from a failing batch", len(results) == 6, f"{len(results)}/6")
check("it actually split rather than giving up", max(intel.llm.sizes) == 6 and min(intel.llm.sizes) <= 2,
      str(intel.llm.sizes))

# A single role that cannot be judged must not take others down with it.
class _AlwaysFails:
    def complete_json(self, *a, **k): raise LLMError("nope")
intel.llm = _AlwaysFails()
results2 = {}
intel._judge_batch(batch, results2, label="t2")
check("an unjudgeable batch leaves no partial verdicts", results2 == {}, str(results2))

# ---- the cacheable prefix really is stable ------------------------------
real = JobIntel(Path(REPO / "workspace/default"), max_live_roles=0) if (REPO / "workspace/default/me/profile.md").exists() else None
if real:
    b1 = [dict(batch[0])]; b2 = [dict(batch[1]), dict(batch[2])]
    check("the fit prefix is identical regardless of batch", real._fit_prefix() == real._fit_prefix())
    check("the variable part differs by batch", real._fit_roles(b1) != real._fit_roles(b2))
    check("prefix + roles is the whole prompt",
          real._fit_prompt(b1) == real._fit_prefix() + "\n\n" + real._fit_roles(b1))
    check("the prefix is big enough to be worth caching",
          len(real._fit_prefix()) >= llm_mod.CACHE_MIN_PREFIX_CHARS, str(len(real._fit_prefix())))
    check("no role text leaks into the cacheable prefix", "r0" not in real._fit_prefix())

if fails:
    print(f"\n{len(fails)} failed: {fails}")
    sys.exit(1)
print("\nall passed")

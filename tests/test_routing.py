"""Unit tests for routing + the LLM fallback ladder. No network."""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "agents"))

import routing
from llm import LLM, LLMError
from job_intel import make_fit_validator, make_cat_validator, make_sector_validator

fails = []


def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)


# --- ladder order ---------------------------------------------------------
ladder = routing.ladder_for_tag("org-sectors")
check("org-sectors starts at T1", ladder[0].startswith("openrouter/qwen/qwen3-30b"), ladder[0])
_pol = routing.load_policy()
_order = routing.tier_order(_pol)
_frontier_tier = _order[-1] if _order else None
_frontier_models = _pol.get("tiers", {}).get(_frontier_tier, {}).get("models", []) if _frontier_tier else []
check("ladder ends at the policy's actual last tier", bool(_frontier_models) and ladder[-1] == _frontier_models[0],
      f"{ladder[-1]} (tier_order={_order})")
check("escalate walks every tier in tier_order, not a hardcoded count",
      routing.escalate(_order[-1], _pol) is None and all(
          routing.escalate(_order[i], _pol) == _order[i + 1] for i in range(len(_order) - 1)),
      str(_order))
check("job-fit is unrouted", routing.ladder_for_tag("job-fit-16") == [])
check("unknown tag is unrouted", routing.ladder_for_tag("mystery") == [])

# --- escalation walks the whole ladder, retrying once per model ------------
calls = []
llm = LLM(BASE)
llm._dispatch = lambda prompt, selector: calls.append(selector) or "not json at all"
try:
    llm.complete_json("x", tag="org-sectors", force=True)
    check("bad output raises", False)
except LLMError as exc:
    check("bad output raises", True)
retries = routing.load_policy().get("retries_before_escalation", 1)
check("tried every model", [c for i, c in enumerate(calls) if i % (retries + 1) == 0] == ladder, str(len(calls)))
check("retried each model", len(calls) == len(ladder) * (retries + 1), f"{len(calls)} calls")

# --- a valid answer from the first model stops the ladder -----------------
calls2 = []
llm2 = LLM(BASE)
llm2._dispatch = lambda p, s: calls2.append(s) or '{"Acme": "Tech - SaaS"}'
out = llm2.complete_json("x", tag="org-sectors", force=True,
                         validate=make_sector_validator(["Acme"]))
check("first model wins", calls2 == ladder[:1], str(calls2))
check("payload returned", out == {"Acme": "Tech - SaaS"})

# --- validator rejects a shape-broken answer, then a good one is accepted --
calls3 = []
answers = iter(['{"Acme": "TechSaaS"}', '{"Acme": "Tech - SaaS"}'])
llm3 = LLM(BASE)
llm3._dispatch = lambda p, s: calls3.append(s) or next(answers)
out3 = llm3.complete_json("y", tag="org-sectors", force=True,
                          validate=make_sector_validator(["Acme"]))
check("bad shape rejected then retried", len(calls3) == 2, str(calls3))
check("second answer accepted", out3 == {"Acme": "Tech - SaaS"})

# --- fit validator --------------------------------------------------------
batch = [{"id": "a"}, {"id": "b"}]
fitv = make_fit_validator(batch)
good = [{"id": i, "fit_score": 40, "seniority_read": "x", "why": "x", "top_gaps": [],
         "what_theyre_really_hiring_for": "x", "pitch_angle": None,
         "recommendation": "skip"} for i in ("a", "b")]
check("fit validator accepts good", fitv(good))
check("fit validator rejects score 140", not fitv([dict(good[0], fit_score=140), good[1]]))
check("fit validator rejects bad recommendation", not fitv([dict(good[0], recommendation="maybe"), good[1]]))
check("fit validator rejects empty", not fitv([]))
check("fit validator unwraps {roles: [...]}", fitv({"roles": good}))

# --- cat validator --------------------------------------------------------
catv = make_cat_validator(batch, {"01", "02"})
check("cat validator accepts none", catv([{"id": "a", "archetype": "none", "confidence": "low"},
                                          {"id": "b", "archetype": "01", "confidence": "high"}]))
check("cat validator rejects unknown id", not catv([{"id": "a", "archetype": "99", "confidence": "high"},
                                                    {"id": "b", "archetype": "01", "confidence": "high"}]))

# --- cache key is unchanged from the pre-routing client -------------------
import hashlib
legacy = hashlib.sha256("job-fit-16|default|PROMPT".encode()).hexdigest()[:32]
check("cache key backward compatible", LLM(BASE)._key("PROMPT", "job-fit-16") == legacy)

# --- claude CLI model strings never carry a provider prefix ---------------
# The T4 fallback in the machine-wide policy carries "anthropic/claude-sonnet-5".
# The CLI itself 404s on a provider-prefixed --model value; it wants the bare
# name. This exercises the REAL dispatch code, not a re-implementation of it.
import subprocess as _subprocess
captured_cmds = []
real_run = _subprocess.run
def _fake_run(cmd, **kwargs):
    captured_cmds.append(cmd)
    class R: returncode = 1; stdout = ""; stderr = "stubbed, no real call made"
    return R()
_subprocess.run = _fake_run
try:
    llm4 = LLM(BASE)
    llm4._dispatch = LLM._dispatch.__get__(llm4)   # real dispatch/claude path
    try:
        llm4.complete_json("x", tag="org-sectors", force=True,
                           validate=lambda data: False)   # force full escalation to T4
    except LLMError:
        pass
finally:
    _subprocess.run = real_run

claude_cmds = [c for c in captured_cmds if c[0] == "claude"]
check("the claude CLI was reached (T4 escalation happened)", bool(claude_cmds), str(len(claude_cmds)))
if claude_cmds:
    model_args = [c[c.index("--model") + 1] for c in claude_cmds if "--model" in c]
    check("a --model value was passed", bool(model_args), str(claude_cmds))
    check("no --model value carries a provider prefix", all("/" not in m for m in model_args), str(model_args))

# --- disabling routing sends everything back to the CLI -------------------
orig = routing.load_tagmap
routing.load_tagmap = lambda: ({"org-sectors": "T1"}, {"enabled": False})
check("kill switch works", routing.ladder_for_tag("org-sectors") == [])
routing.load_tagmap = orig

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)

"""The tier ladder comes from .env; the tag map from config/model-routing.toml."""
import os, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agents"))

import routing

fails = []
def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

saved = dict(os.environ)
try:
    for k in [k for k in os.environ if k.startswith("MC_MODEL_") or k == "MC_TIER_ORDER"]:
        os.environ.pop(k)

    # Models come from the environment, one per tier.
    os.environ["MC_TIER_ORDER"] = "T1,T2,T3"
    os.environ["MC_MODEL_T1"] = "openrouter/cheap-1"
    os.environ["MC_MODEL_T2"] = "openrouter/mid-1"
    os.environ["MC_MODEL_T3"] = "frontier-1"
    pol = routing.load_policy()
    check("policy is built from the environment", pol["enabled"] and pol["tier_order"] == ["T1", "T2", "T3"],
          str(pol["tier_order"]))
    check("each tier carries its configured model",
          [pol["tiers"][t]["models"] for t in ("T1", "T2", "T3")]
          == [["openrouter/cheap-1"], ["openrouter/mid-1"], ["frontier-1"]], str(pol["tiers"]))

    # A tag starts at its configured tier and escalates upward to the frontier.
    ladder = routing.ladder_for_tag("org-sectors")
    check("ladder starts at the tag's tier and ends at the last tier",
          ladder and ladder[0] == "openrouter/cheap-1" and ladder[-1] == "frontier-1", str(ladder))

    # Several models in one tier are allowed, comma separated.
    os.environ["MC_MODEL_T1"] = "openrouter/cheap-1, openrouter/cheap-2"
    check("a tier may list several models",
          routing.load_policy()["tiers"]["T1"]["models"] == ["openrouter/cheap-1", "openrouter/cheap-2"],
          str(routing.load_policy()["tiers"]["T1"]["models"]))

    # No models configured must degrade to "unrouted", never raise.
    for k in ("MC_MODEL_T1", "MC_MODEL_T2", "MC_MODEL_T3"):
        os.environ.pop(k, None)
    pol = routing.load_policy()
    check("no configured models means routing is off", not pol["enabled"], str(pol))
    check("an unrouted tag yields an empty ladder", routing.ladder_for_tag("org-sectors") == [])

    # The machine-wide Prime Agent policy must no longer be consulted at all.
    src = Path(routing.__file__).read_text()
    for gone in ("PRIME_COMPUTE_ROUTING_CONFIG", "DEFAULT_POLICY", ".prime/agent"):
        check(f"routing no longer references {gone}", gone not in src)
    llm_src = (REPO / "agents/llm.py").read_text()
    check("llm no longer reads the global auth.json", ".prime/agent" not in llm_src)
finally:
    os.environ.clear()
    os.environ.update(saved)

# The committed tag map must name a tier for the calls the pipeline makes.
tags, data = routing.load_tagmap()
check("the tag map is enabled", data.get("enabled", True))
for tag in ("job-fit", "role-cat", "org-sectors", "content-radar"):
    check(f"{tag} is assigned a tier", bool(routing.tier_for_tag(tag)), str(tags.get(tag)))
check("the tag map carries no model names (those live in .env)",
      not any("/" in str(v) or "claude" in str(v) for v in tags.values()), str(tags))

# --- regression: the CLI sentinel must never be sent as an API model ------
# This is the failure the user hit: every fit batch died with
#   anthropic api 404: {"type":"not_found_error","message":"model: claude-cli-default"}
# because an unrouted tag resolved to the sentinel and the API branch passed it
# straight through as a model id.
sys.path.insert(0, str(REPO / "agents"))
import llm as llm_mod

sent = {}
class _Resp:
    status_code = 200
    def json(self):
        return {"content": [{"text": '{"ok": true}'}],
                "usage": {"input_tokens": 1, "output_tokens": 1}}

def _fake_post(url, headers=None, json=None, timeout=None):
    sent["model"] = (json or {}).get("model")
    return _Resp()

real_post = llm_mod.requests.post
saved_backend = os.environ.get("LLM_BACKEND")
llm_mod.requests.post = _fake_post
os.environ["LLM_BACKEND"] = "api"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
try:
    client = llm_mod.LLM(REPO)
    client._call_anthropic_api("x", llm_mod.CLI_DEFAULT)
    check("the CLI sentinel is translated, never sent as a model id",
          sent["model"] != llm_mod.CLI_DEFAULT, str(sent["model"]))
    check("it falls back to a real model id", sent["model"] == llm_mod.DEFAULT_API_MODEL, str(sent["model"]))

    # A real routed name still passes through, minus any provider prefix.
    client._call_anthropic_api("x", "anthropic/claude-sonnet-5")
    check("a provider prefix is stripped for the API", sent["model"] == "claude-sonnet-5", str(sent["model"]))
finally:
    llm_mod.requests.post = real_post
    if saved_backend is None:
        os.environ.pop("LLM_BACKEND", None)
    else:
        os.environ["LLM_BACKEND"] = saved_backend

if fails:
    print(f"\n{len(fails)} failed: {fails}")
    sys.exit(1)
print("\nall passed")

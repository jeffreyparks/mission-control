"""
Model routing for Mission Control.

Two files, one job each, both owned by this project:

  .env                        WHICH MODEL each tier is, machine-wide, beside the
                              API keys that pay for them:
                                  MC_TIER_ORDER=T1,T2,T3,T4
                                  MC_MODEL_T1=openrouter/qwen/qwen3-30b-...
                                  MC_MODEL_T4=claude-opus-5
                              One model per tier is the simple case; a comma
                              separated list is allowed if you want more than
                              one attempt inside a tier.

  config/model-routing.toml   WHICH TIER each call starts at, versioned with the
                              pipeline: "job-fit" = "T3", and so on.

Model names are transport agnostic. An "openrouter/" prefix picks OpenRouter;
anything else is a Claude model reached either through the claude CLI or the
Anthropic API, depending on LLM_BACKEND. So LLM_BACKEND chooses who bills you,
never which model answers.

If no MC_MODEL_* is configured, or the tag map is disabled, every lookup returns
an empty ladder and callers fall back to the default Claude model. Routing must
never be able to break the daily run.
"""
import os
import tomllib
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

REPO_TAGMAP = BASE / "config/model-routing.toml"

# Tier ladder used when MC_TIER_ORDER is not set.
_DEFAULT_TIER_ORDER = ["T1", "T2", "T3", "T4"]

# Used when config/model-routing.toml is absent.
FALLBACK_TAGS = {
    "job-fit": "T3",
    "role-cat": "T1",
    "org-sectors": "T1",
    "content-radar": "T2",
}

def tier_order(policy=None):
    policy = policy if policy is not None else load_policy()
    order = policy.get("tier_order")
    if isinstance(order, list) and order and all(isinstance(t, str) for t in order):
        return list(order)
    return list(_DEFAULT_TIER_ORDER)


def _read_toml(path):
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:  # noqa: BLE001 - routing must degrade, never raise
        return {}


def _models_for(tier):
    """MC_MODEL_<TIER> - one selector, or several separated by commas."""
    raw = os.environ.get(f"MC_MODEL_{tier.upper()}", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def load_policy():
    """Build the tier ladder from the environment.

    Returns the same shape the rest of this module already expects, so only the
    source changed: the models now come from .env beside the keys that pay for
    them, instead of a machine-wide file owned by another tool.
    """
    raw_order = os.environ.get("MC_TIER_ORDER", "")
    order = [t.strip() for t in raw_order.split(",") if t.strip()] or list(_DEFAULT_TIER_ORDER)

    tiers = {}
    for tier in order:
        models = _models_for(tier)
        if models:
            tiers[tier] = {"models": models}

    # No models configured means no routing - callers fall back to the default
    # Claude model rather than erroring.
    return {"enabled": bool(tiers), "tier_order": order, "tiers": tiers}


def load_tagmap():
    data = _read_toml(REPO_TAGMAP)
    tags = data.get("tags")
    if not isinstance(tags, dict) or not tags:
        return dict(FALLBACK_TAGS), data
    return tags, data


def tier_for_tag(tag):
    """Map an LLM tag to a tier. Longest matching prefix wins."""
    tags, _ = load_tagmap()
    best = None
    for prefix, tier in tags.items():
        if tag == prefix or tag.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, tier)
    return best[1] if best else None


def escalate(tier, policy=None):
    order = tier_order(policy)
    try:
        idx = order.index(tier)
    except ValueError:
        return None
    return order[idx + 1] if idx + 1 < len(order) else None


def ladder_for_tag(tag, policy=None):
    """Ordered model selectors to try for this tag: the tag's tier, then every
    higher tier. The last entry is always the frontier fallback, so a cheap model
    that fails JSON parsing still ends with a correct answer."""
    policy = policy if policy is not None else load_policy()
    if not policy or not policy.get("enabled", False):
        return []

    _, tagdata = load_tagmap()
    if not tagdata.get("enabled", True):
        return []

    tier = tier_for_tag(tag)
    if not tier:
        return []

    tiers = policy.get("tiers", {})
    ladder = []
    cur = tier
    while cur:
        for selector in tiers.get(cur, {}).get("models", []):
            if selector not in ladder:
                ladder.append(selector)
        cur = escalate(cur, policy)   # pass policy through: one file read, not one per hop

    per_tier = tagdata.get("max_models_per_tier")
    if isinstance(per_tier, int) and per_tier > 0:
        trimmed, seen = [], {}
        for selector in ladder:
            key = tier_of_selector(selector, policy)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= per_tier:
                trimmed.append(selector)
        ladder = trimmed
    return ladder


def tier_of_selector(selector, policy=None):
    policy = policy if policy is not None else load_policy()
    for tier, block in (policy.get("tiers") or {}).items():
        if selector in (block.get("models") or []):
            return tier
    return "?"


def describe(tag):
    """Human-readable routing decision, for logs and --explain."""
    tier = tier_for_tag(tag)
    ladder = ladder_for_tag(tag)
    if not ladder:
        return f"{tag}: routing off -> claude CLI default"
    return f"{tag}: tier {tier} -> " + " | ".join(
        f"{s} [{tier_of_selector(s)}]" for s in ladder
    )

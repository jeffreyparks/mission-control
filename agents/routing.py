"""
Model routing for Mission Control.

Reads the machine-wide compute-routing policy that Prime Agent already uses:

    ~/.prime/agent/skills/compute-routing/config.toml
    (override with PRIME_COMPUTE_ROUTING_CONFIG)

That file owns the tier ladder (T1..T4) and the ordered model selectors in each
tier. This module adds the one thing the pipeline needs on top: a map from an
LLM call tag ("job-fit-16", "org-sectors", ...) to a tier.

The tag map lives in the repo at config/model-routing.toml so pipeline routing
is versioned with the pipeline, while the model lists stay machine-wide.

If the policy file is missing, unreadable, or disabled, every lookup returns an
empty ladder and callers fall back to the default claude CLI. Routing must never
be able to break the daily run.
"""
import os
import tomllib
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

DEFAULT_POLICY = Path.home() / ".prime/agent/skills/compute-routing/config.toml"
REPO_TAGMAP = BASE / "config/model-routing.toml"

# Used when config/model-routing.toml is absent.
FALLBACK_TAGS = {
    "job-fit": "T3",
    "role-cat": "T1",
    "org-sectors": "T1",
    "content-radar": "T2",
}

_TIER_ORDER = ["T1", "T2", "T3", "T4"]


def _read_toml(path):
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:  # noqa: BLE001 - routing must degrade, never raise
        return {}


def policy_path():
    override = os.environ.get("PRIME_COMPUTE_ROUTING_CONFIG")
    return Path(override) if override else DEFAULT_POLICY


def load_policy():
    return _read_toml(policy_path())


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


def escalate(tier):
    try:
        idx = _TIER_ORDER.index(tier)
    except ValueError:
        return None
    return _TIER_ORDER[idx + 1] if idx + 1 < len(_TIER_ORDER) else None


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
        cur = escalate(cur)

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

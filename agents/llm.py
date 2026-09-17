"""
LLM client for Mission Control.

Three transports, chosen by the model name and LLM_BACKEND:
  - the local `claude` CLI in headless mode (no API key needed)  LLM_BACKEND=cli
  - the Anthropic Messages API, billed per token                 LLM_BACKEND=api
  - OpenRouter over HTTPS, for "openrouter/" selectors

Which model runs is decided per call by agents/routing.py, from the tier ladder
in .env and the tag map in config/model-routing.toml - this project owns both.
A cheap model is tried first; if it fails to return usable JSON the client walks
up the tier ladder and ends at the frontier model, so quality is never silently
traded away.

Results are cached by content hash so we never pay twice for the same input.
"""
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import routing

# Repo-root .env, not cwd-relative - every entry point (run_daily.py, dashboard.py,
# setup.py, a bare `python agents/llm.py`) must see the same secrets regardless
# of where it was launched from. Never overrides an already-exported env var.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Sentinel for "no routed model - let the transport pick". The CLI understands
# this by simply being given no --model; the API cannot, so it is translated to
# DEFAULT_API_MODEL before any request is built.
CLI_DEFAULT = "claude-cli-default"
DEFAULT_API_MODEL = "claude-sonnet-5"

# Output budget. A fixed cap silently truncates the moment a caller asks for
# more than it allows - which is how a 16-role fit batch died mid-JSON while
# both models in the ladder "failed" for what looked like a quality reason.
# Callers size their own budget with output_budget(); this is only the floor for
# callers that do not.
DEFAULT_MAX_TOKENS = 4096
MAX_TOKENS_CEILING = 16000

# Anthropic caches a stable prompt prefix and charges ~10% to read it back.
# Worth it only above a minimum prefix size, which the API enforces anyway.
CACHE_MIN_PREFIX_CHARS = 4000

STRICTER = (
    "\n\nIMPORTANT: your previous answer could not be parsed. "
    "Return ONLY raw JSON. No prose, no markdown fences, no explanation."
)


class LLMError(RuntimeError):
    pass


class TruncatedResponse(LLMError):
    """The model ran out of output budget mid-answer.

    Distinct from unparseable output because the remedies are opposite: a
    chatty model is worth retrying with a stricter instruction, while a
    truncated one needs a bigger budget or a smaller request. Retrying or
    escalating a truncation just spends a second, larger bill on the same
    ceiling."""


def api_cost(model_id, usage):
    """USD for one Anthropic API call, or None when the model has no price.

    None is deliberately not 0.0: an unpriced model should show up as unknown
    spend, not as free."""
    prices = routing.load_prices().get(model_id)
    if not prices:
        return None
    per_million = lambda n, rate: (n or 0) / 1_000_000 * rate   # noqa: E731
    base_in = prices.get("input", 0.0)
    return (per_million(usage.get("input_tokens"), base_in)
            + per_million(usage.get("output_tokens"), prices.get("output", 0.0))
            + per_million(usage.get("cache_creation_input_tokens"),
                          prices.get("cache_write", base_in * 1.25))
            + per_million(usage.get("cache_read_input_tokens"),
                          prices.get("cache_read", base_in * 0.10)))


def _join_prompt(cache_prefix, prompt):
    """The full prompt text, for transports that take one string."""
    return f"{cache_prefix}\n\n{prompt}" if cache_prefix else prompt


def output_budget(n_items, per_item=700, overhead=500, ceiling=MAX_TOKENS_CEILING):
    """Output tokens to allow for a request covering n_items.

    per_item=700 is ~1.5x the largest real fit verdict measured over 499 roles
    (median 216, p90 307, max 480), so a normal batch has generous headroom and
    an unusually wordy one still fits."""
    return max(DEFAULT_MAX_TOKENS, min(ceiling, overhead + per_item * max(1, n_items)))


def _openrouter_key():
    return os.environ.get("OPENROUTER_API_KEY")


def _anthropic_api_key():
    return os.environ.get("ANTHROPIC_API_KEY")


def llm_backend():
    """Explicit choice, read from .env / the environment. "cli" (default) uses
    the local claude CLI under your Claude subscription; "api" bills
    ANTHROPIC_API_KEY per token. No silent auto-fallback between the two."""
    return (os.environ.get("LLM_BACKEND") or "cli").strip().lower()


class LLM:
    def __init__(self, base_dir, model=None, timeout=600, route=True):
        self.base_dir = Path(base_dir)
        self.cache_dir = self.base_dir / "data/llm-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.model = model          # explicit pin; disables routing
        self.timeout = timeout
        self.route = route and model is None
        self.stats = {"hits": 0, "misses": 0, "cost_usd": 0.0, "by_model": {},
                       "cache_write_tokens": 0, "cache_read_tokens": 0,
                       "api_input_tokens": 0, "api_output_tokens": 0,
                       "unpriced_models": set()}

    # ---------- cache ----------

    def _key(self, prompt, tag):
        # The literal "default" is kept for backward compatibility: it keeps
        # every verdict cached before routing existed valid and free. The cache
        # is keyed by the INPUT, not by which model answered it.
        raw = f"{tag}|default|{prompt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _cache_path(self, key):
        return self.cache_dir / f"{key}.json"

    # ---------- parsing ----------

    @staticmethod
    def _extract_json(text):
        """Pull the first JSON object or array out of a model response."""
        text = (text or "").strip()
        # Open-weight reasoning models often emit a <think> block first.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
        raise LLMError(f"could not parse JSON from response: {text[:400]}")

    # ---------- backends ----------

    def _call_claude(self, prompt, model=None):
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        if model and model != CLI_DEFAULT:
            # A selector may carry a provider prefix ("anthropic/claude-sonnet-5"),
            # which the CLI itself rejects with a 404 - it wants the bare model
            # name or alias. Stripping here keeps the fallback ladder working
            # whatever form the configured name takes.
            cli_model = model.split("/", 1)[-1] if "/" in model else model
            cmd += ["--model", cli_model]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.timeout, cwd=str(self.base_dir),
            )
        except subprocess.TimeoutExpired:
            raise LLMError(f"claude CLI timed out after {self.timeout}s")
        except FileNotFoundError:
            raise LLMError(
                "claude CLI not found. Install it and run `claude login`, or "
                "set LLM_BACKEND=api and ANTHROPIC_API_KEY in .env instead."
            )

        if proc.returncode != 0:
            raise LLMError(f"claude CLI failed ({proc.returncode}): {proc.stderr[:400]}")
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise LLMError(f"bad CLI envelope: {proc.stdout[:400]}")
        if envelope.get("is_error"):
            raise LLMError(f"claude returned an error: {str(envelope)[:400]}")

        self.stats["cost_usd"] += float(envelope.get("total_cost_usd") or 0.0)
        return envelope.get("result", "")

    def _call_anthropic_api(self, prompt, model=None, max_tokens=None, cache_prefix=None):
        """Direct Anthropic Messages API call. Alternative to _call_claude when
        LLM_BACKEND=api - billed per token instead of riding the CLI subscription.

        cache_prefix goes in a cached system block: a stable prefix (the
        candidate context and the scoring rules, identical for every batch in a
        run) is uploaded once and read back at ~10% of the price, which is what
        makes small, higher-quality batches affordable."""
        key = _anthropic_api_key()
        if not key:
            raise LLMError(
                "LLM_BACKEND=api but ANTHROPIC_API_KEY is not set in .env"
            )
        # "claude-cli-default" means "whatever the CLI would pick" - it is a
        # sentinel, not a model name, and the API 404s on it. Any unrouted tag
        # arrives here holding it, so translate before building the request.
        if model == CLI_DEFAULT:
            model = None
        model_id = model.split("/", 1)[-1] if model and "/" in model else (model or DEFAULT_API_MODEL)
        budget = max_tokens or DEFAULT_MAX_TOKENS

        payload = {
            "model": model_id,
            "max_tokens": budget,
            "messages": [{"role": "user", "content": prompt}],
        }
        if cache_prefix:
            block = {"type": "text", "text": cache_prefix}
            if len(cache_prefix) >= CACHE_MIN_PREFIX_CHARS:
                block["cache_control"] = {"type": "ephemeral"}
            payload["system"] = [block]

        try:
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LLMError(f"anthropic api request failed: {exc}")

        if resp.status_code != 200:
            raise LLMError(f"anthropic api {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        try:
            content = "".join(
                block.get("text", "") for block in body.get("content", [])
                if block.get("type") == "text"
            )
        except Exception:  # noqa: BLE001
            raise LLMError(f"unexpected anthropic body: {str(body)[:300]}")

        usage = body.get("usage") or {}
        self.stats["cache_write_tokens"] += usage.get("cache_creation_input_tokens") or 0
        self.stats["cache_read_tokens"] += usage.get("cache_read_input_tokens") or 0
        self.stats["api_input_tokens"] += usage.get("input_tokens") or 0
        self.stats["api_output_tokens"] += usage.get("output_tokens") or 0

        # The API bills per token but reports none of it in dollars, so price it
        # here from the table in config/model-routing.toml.
        cost = api_cost(model_id, usage)
        if cost is None:
            self.stats["unpriced_models"].add(model_id)
        else:
            self.stats["cost_usd"] += cost

        # Truncation is reported, not inferred from a JSON parse failure, so the
        # caller can shrink the request instead of paying twice for the same
        # ceiling. Raised even though `content` holds a partial answer: half a
        # verdict is not a verdict.
        if body.get("stop_reason") == "max_tokens":
            raise TruncatedResponse(
                f"response hit the {budget}-token output budget and was cut off "
                f"({usage.get('output_tokens', '?')} output tokens); "
                f"send fewer items per call or raise the budget"
            )
        return content


    def _call_openrouter(self, prompt, selector, max_tokens=None):
        key = _openrouter_key()
        if not key:
            raise LLMError("no OpenRouter key - set OPENROUTER_API_KEY in .env")
        model_id = selector.split("/", 1)[1]  # strip the "openrouter/" prefix
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {key}",
                         "X-Title": "mission-control"},
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
                    "usage": {"include": True},
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LLMError(f"openrouter request failed: {exc}")

        if resp.status_code != 200:
            raise LLMError(f"openrouter {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if "error" in body:
            raise LLMError(f"openrouter error: {str(body['error'])[:300]}")
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise LLMError(f"unexpected openrouter body: {str(body)[:300]}")

        usage = body.get("usage") or {}
        self.stats["cost_usd"] += float(usage.get("cost") or 0.0)
        return content

    def _dispatch(self, prompt, selector, max_tokens=None, cache_prefix=None):
        """prompt is the VARIABLE part; cache_prefix is the stable part. Only the
        Anthropic path can cache a prefix, so the other transports just receive
        the two concatenated - identical text, no behaviour change."""
        if selector.startswith("openrouter/"):
            return self._call_openrouter(_join_prompt(cache_prefix, prompt), selector, max_tokens)
        # anthropic/* selectors and the CLI_DEFAULT sentinel both
        # target Claude; LLM_BACKEND picks which transport reaches it. This
        # is an explicit choice, never a silent runtime fallback.
        if llm_backend() == "api":
            return self._call_anthropic_api(prompt, selector, max_tokens, cache_prefix)
        return self._call_claude(_join_prompt(cache_prefix, prompt), selector)

    # ---------- core call ----------

    def complete_json(self, prompt, tag="generic", force=False, validate=None,
                       max_tokens=None, cache_prefix=None):
        """Send a prompt, return parsed JSON. Cached.

        validate: optional callable(data) -> bool. A model whose output fails it
        is treated exactly like unparseable output: retry, then escalate.
        max_tokens: output budget; see output_budget() for sizing by batch.
        cache_prefix: stable leading text, cached by the Anthropic backend. The
        response cache key covers prefix and prompt together, so splitting a
        prompt this way never changes which entry it hits.
        """
        key = self._key(_join_prompt(cache_prefix, prompt), tag)
        path = self._cache_path(key)

        if path.exists() and not force:
            self.stats["hits"] += 1
            return json.loads(path.read_text())["data"]

        self.stats["misses"] += 1

        if self.model:
            ladder = [self.model]
        elif self.route:
            ladder = routing.ladder_for_tag(tag) or [CLI_DEFAULT]
        else:
            ladder = [CLI_DEFAULT]

        retries = 1
        policy = routing.load_policy()
        if isinstance(policy.get("retries_before_escalation"), int):
            retries = policy["retries_before_escalation"]

        errors = []
        for selector in ladder:
            for attempt in range(retries + 1):
                text = prompt if attempt == 0 else prompt + STRICTER
                started = time.time()
                try:
                    raw = self._dispatch(text, selector, max_tokens, cache_prefix)
                    data = self._extract_json(raw)
                    if validate and not validate(data):
                        raise LLMError("failed caller validation")
                except TruncatedResponse as exc:
                    # Neither a stricter instruction nor a bigger model buys more
                    # room, so stop here and let the caller split the request.
                    raise TruncatedResponse(f"{selector}: {exc}")
                except LLMError as exc:
                    errors.append(f"{selector} try{attempt + 1}: {exc}")
                    continue

                stat = self.stats["by_model"].setdefault(
                    selector, {"calls": 0, "seconds": 0.0})
                stat["calls"] += 1
                stat["seconds"] += time.time() - started

                path.write_text(json.dumps(
                    {"tag": tag, "model": selector, "data": data}, indent=2))
                return data

        raise LLMError(f"all models failed for tag {tag}: " + " ; ".join(errors[-3:]))

    def report(self):
        line = (f"LLM: {self.stats['misses']} calls, {self.stats['hits']} cached, "
                f"${self.stats['cost_usd']:.3f}")
        if self.stats["by_model"]:
            parts = [f"{s.split('/')[-1]} x{v['calls']}"
                     for s, v in self.stats["by_model"].items()]
            line += " [" + ", ".join(parts) + "]"

        api_in = self.stats.get("api_input_tokens") or 0
        api_out = self.stats.get("api_output_tokens") or 0
        read = self.stats.get("cache_read_tokens") or 0
        written = self.stats.get("cache_write_tokens") or 0
        if api_in or api_out:
            line += f" (api {api_in:,} in / {api_out:,} out"
            if read or written:
                # Cached reads are the cheap part; showing them makes the saving
                # visible instead of hiding inside the input count.
                line += f", cache {read:,} read / {written:,} written"
            line += ")"
        elif read or written:
            line += f" (prompt cache: {read:,} read, {written:,} written)"

        unpriced = self.stats.get("unpriced_models") or set()
        if unpriced:
            line += (f" [cost excludes unpriced {', '.join(sorted(unpriced))} - "
                     f"add it to [prices] in config/model-routing.toml]")
        return line

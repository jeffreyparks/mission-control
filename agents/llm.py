"""
LLM client for Mission Control.

Two backends:
  - the local `claude` CLI in headless mode (no API key needed)
  - OpenRouter over HTTPS, for cheap open-weight models

Which one runs is decided per call by agents/routing.py, which reads the same
machine-wide compute-routing policy Prime Agent uses. A cheap model is tried
first; if it fails to return usable JSON the client walks up the tier ladder and
ends at the frontier model, so quality is never silently traded away.

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

# Repo-root .env, not cwd-relative - every entry point (run_daily.py, serve.py,
# setup.py, a bare `python agents/llm.py`) must see the same secrets regardless
# of where it was launched from. Never overrides an already-exported env var.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
AUTH_FILE = Path.home() / ".prime/agent/auth.json"

STRICTER = (
    "\n\nIMPORTANT: your previous answer could not be parsed. "
    "Return ONLY raw JSON. No prose, no markdown fences, no explanation."
)


class LLMError(RuntimeError):
    pass


def _openrouter_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        return json.loads(AUTH_FILE.read_text())["openrouter"]["key"]
    except Exception:  # noqa: BLE001
        return None


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
        self.stats = {"hits": 0, "misses": 0, "cost_usd": 0.0, "by_model": {}}

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
        if model and model != "claude-cli-default":
            # The compute-routing policy carries a provider prefix ("anthropic/
            # claude-sonnet-5"), which the CLI itself rejects with a 404 - it
            # wants the bare model name or alias. Escalating to T4 without this
            # strip fails outright, silently defeating the whole fallback ladder.
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

    def _call_anthropic_api(self, prompt, model=None):
        """Direct Anthropic Messages API call. Alternative to _call_claude when
        LLM_BACKEND=api - billed per token instead of riding the CLI subscription."""
        key = _anthropic_api_key()
        if not key:
            raise LLMError(
                "LLM_BACKEND=api but ANTHROPIC_API_KEY is not set in .env"
            )
        model_id = model.split("/", 1)[-1] if model and "/" in model else (model or "claude-sonnet-5")
        try:
            resp = requests.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": model_id,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
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

        # complete_json() already records calls/seconds per selector after
        # dispatch returns; the Anthropic API gives token counts, not USD, so
        # cost_usd (unlike the CLI and OpenRouter paths) is not incremented here.
        return content

    def _call_openrouter(self, prompt, selector):
        key = _openrouter_key()
        if not key:
            raise LLMError("no OpenRouter key (env OPENROUTER_API_KEY or ~/.prime/agent/auth.json)")
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

    def _dispatch(self, prompt, selector):
        if selector.startswith("openrouter/"):
            return self._call_openrouter(prompt, selector)
        # anthropic/* selectors and the "claude-cli-default" sentinel both
        # target Claude; LLM_BACKEND picks which transport reaches it. This
        # is an explicit choice, never a silent runtime fallback.
        if llm_backend() == "api":
            return self._call_anthropic_api(prompt, selector)
        return self._call_claude(prompt, selector)

    # ---------- core call ----------

    def complete_json(self, prompt, tag="generic", force=False, validate=None):
        """Send a prompt, return parsed JSON. Cached.

        validate: optional callable(data) -> bool. A model whose output fails it
        is treated exactly like unparseable output: retry, then escalate.
        """
        key = self._key(prompt, tag)
        path = self._cache_path(key)

        if path.exists() and not force:
            self.stats["hits"] += 1
            return json.loads(path.read_text())["data"]

        self.stats["misses"] += 1

        if self.model:
            ladder = [self.model]
        elif self.route:
            ladder = routing.ladder_for_tag(tag) or ["claude-cli-default"]
        else:
            ladder = ["claude-cli-default"]

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
                    raw = self._dispatch(text, selector)
                    data = self._extract_json(raw)
                    if validate and not validate(data):
                        raise LLMError("failed caller validation")
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
        return line

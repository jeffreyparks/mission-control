"""
LLM client for Mission Control.

Uses the local `claude` CLI in headless mode. No API key required.
Results are cached by content hash so we never pay twice for the same input.
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_dir, model=None, timeout=600):
        self.base_dir = Path(base_dir)
        self.cache_dir = self.base_dir / "data/llm-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.timeout = timeout
        self.stats = {"hits": 0, "misses": 0, "cost_usd": 0.0}

    # ---------- cache ----------

    def _key(self, prompt, tag):
        raw = f"{tag}|{self.model or 'default'}|{prompt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _cache_path(self, key):
        return self.cache_dir / f"{key}.json"

    # ---------- parsing ----------

    @staticmethod
    def _extract_json(text):
        """Pull the first JSON object or array out of a model response."""
        text = text.strip()
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

    # ---------- core call ----------

    def complete_json(self, prompt, tag="generic", force=False):
        """Send a prompt, return parsed JSON. Cached."""
        key = self._key(prompt, tag)
        path = self._cache_path(key)

        if path.exists() and not force:
            self.stats["hits"] += 1
            return json.loads(path.read_text())["data"]

        self.stats["misses"] += 1
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(self.base_dir),
            )
        except subprocess.TimeoutExpired:
            raise LLMError(f"claude CLI timed out after {self.timeout}s")

        if proc.returncode != 0:
            raise LLMError(f"claude CLI failed ({proc.returncode}): {proc.stderr[:400]}")

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise LLMError(f"bad CLI envelope: {proc.stdout[:400]}")

        if envelope.get("is_error"):
            raise LLMError(f"claude returned an error: {str(envelope)[:400]}")

        self.stats["cost_usd"] += float(envelope.get("total_cost_usd") or 0.0)

        data = self._extract_json(envelope.get("result", ""))
        path.write_text(json.dumps({"tag": tag, "data": data}, indent=2))
        return data

    def report(self):
        return (
            f"LLM: {self.stats['misses']} calls, {self.stats['hits']} cached, "
            f"${self.stats['cost_usd']:.3f}"
        )

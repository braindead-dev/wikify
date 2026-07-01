"""The model provider seam — one call, one swappable backend.

Everything above talks to `LLMClient.complete_json(...)`. The concrete model,
provider, base URL, and reasoning knobs live behind it. Transient failures and
malformed JSON are retried with exponential backoff that honors Retry-After.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

from .config import DEFAULT_MODEL, MODELS, PROVIDERS

_ENV_LOADED = False


def _load_env():
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        from dotenv import load_dotenv
        # repo root is two levels up from this file (atlas/llm/client.py)
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except Exception:
        pass
    _ENV_LOADED = True


def _extract_json(text: str):
    """Pull a JSON value out of a model response (tolerating ``` fences / prose)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # fall back to the outermost {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object found in model output")


class Usage:
    """Cumulative token accounting for a run (observability)."""
    def __init__(self):
        self.calls = self.input = self.output = 0

    def add(self, u):
        if u:
            self.calls += 1
            self.input += getattr(u, "prompt_tokens", 0) or 0
            self.output += getattr(u, "completion_tokens", 0) or 0

    def __str__(self):
        return f"{self.calls} calls · {self.input:,} in · {self.output:,} out tokens"


class LLMClient:
    def __init__(self, model=DEFAULT_MODEL, effort=None, max_retries=4):
        _load_env()
        if model not in MODELS:
            raise KeyError(f"unknown model {model!r}; known: {', '.join(MODELS)}")
        cfg = MODELS[model]
        prov = PROVIDERS[cfg["provider"]]
        key = os.environ.get(prov["key_env"])
        if not key:
            raise RuntimeError(
                f"{prov['key_env']} not set — add it to .env or the environment.")
        from openai import OpenAI
        self._client = OpenAI(base_url=prov["base_url"], api_key=key,
                              default_headers={"X-Title": "chat-wiki"})
        self.name = model
        self.model_id = cfg["model"]
        self.effort = effort if effort is not None else cfg.get("reasoning")
        self.max_retries = max_retries
        self.usage = Usage()

    def complete_json(self, system: str, user: str, effort=None, schema=None, schema_name="output"):
        """Return parsed JSON from the model. When `schema` is given, use genuine
        structured outputs (response_format=json_schema, strict) so the model is
        constrained to the schema at decode time — not merely asked to emit JSON.
        Retries transient errors and malformed JSON with backoff."""
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        eff = effort if effort is not None else self.effort
        extra = {}
        if eff is not None and str(eff).lower() in ("none", "off", "false"):
            extra["reasoning"] = {"enabled": False}          # turn thinking off
        elif eff:
            extra["reasoning"] = {"effort": eff}
        if schema is not None:
            # require a provider that actually enforces the schema, don't silently
            # fall back to a free-form completion.
            extra["provider"] = {"require_parameters": True}
            response_format = {"type": "json_schema",
                               "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
        else:
            response_format = {"type": "json_object"}

        last_err = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model_id, messages=messages,
                    response_format=response_format, extra_body=extra or None,
                )
                self.usage.add(getattr(resp, "usage", None))
                return _extract_json(resp.choices[0].message.content or "")
            except Exception as e:                       # transient API or JSON error
                last_err = e
                delay = _retry_after(e) or (0.4 * (2 ** attempt) * random.uniform(0.9, 1.1))
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
        raise RuntimeError(f"model call failed after {self.max_retries} tries: {last_err}")


def _retry_after(err):
    """Honor a server Retry-After (seconds) when present."""
    resp = getattr(err, "response", None)
    if resp is not None:
        val = getattr(resp, "headers", {}).get("retry-after")
        if val and str(val).isdigit():
            return float(val)
    return None

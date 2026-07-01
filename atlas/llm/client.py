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
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import DEFAULT_MODEL, MODELS, PROVIDERS

_ENV_LOADED = False


class LLMError(RuntimeError):
    """A model call that ultimately failed. Carries the raw output and the
    finish reason so the failure is diagnosable — e.g. finish_reason='length'
    means the response was truncated at the output-token limit."""

    def __init__(self, message, *, finish_reason=None, raw="", attempts=0):
        super().__init__(message)
        self.finish_reason = finish_reason
        self.raw = raw
        self.attempts = attempts


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

    def complete_json(self, system: str, user: str, effort=None, schema=None,
                      schema_name="output", trace=None):
        """Return parsed JSON from the model. When `schema` is given, use genuine
        structured outputs (response_format=json_schema, strict) so the model is
        constrained to the schema at decode time — not merely asked to emit JSON.

        Transient errors are retried with backoff and each retry is logged (not
        silent). A truncated response (finish_reason='length') is not retried —
        it would only truncate again — and fails fast with a clear reason. On
        exhaustion raises `LLMError` carrying the raw output and finish reason.
        If `trace` is given it is called with a full record of the request and
        outcome (model, params, exact prompt, raw output, finish reason)."""
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

        started = time.time()
        meta = {"model": self.model_id, "effort": eff, "schema": schema_name,
                "started_at": datetime.now().isoformat(timespec="seconds")}
        last_err = last_finish = None
        last_raw = ""
        attempt = 0
        for attempt in range(self.max_retries):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model_id, messages=messages,
                    response_format=response_format, extra_body=extra or None,
                )
                self.usage.add(getattr(resp, "usage", None))
                choice = resp.choices[0]
                last_raw = choice.message.content or ""
                last_finish = getattr(choice, "finish_reason", None)
                data = _extract_json(last_raw)
                if trace:
                    trace({**meta, "status": "ok", "attempts": attempt + 1,
                           "finish_reason": last_finish, "duration_s": round(time.time() - started, 2),
                           "usage": _usage_dict(getattr(resp, "usage", None)),
                           "system": system, "user": user, "response": last_raw})
                return data
            except Exception as e:                       # transient API or JSON error
                last_err = e
                if last_finish == "length":              # truncated — retrying won't help
                    break
                if attempt < self.max_retries - 1:
                    print(f"  [llm] retry {attempt + 1}/{self.max_retries}: "
                          f"{type(e).__name__}: {str(e)[:100]}", file=sys.stderr, flush=True)
                    time.sleep(_retry_after(e) or (0.4 * (2 ** attempt) * random.uniform(0.9, 1.1)))

        detail = ("output truncated (finish_reason=length) — chunk too dense; lower chunk_tokens"
                  if last_finish == "length" else str(last_err))
        err = LLMError(f"failed after {attempt + 1} tries: {detail}",
                       finish_reason=last_finish, raw=last_raw, attempts=attempt + 1)
        if trace:
            trace({**meta, "status": "error", "attempts": attempt + 1,
                   "finish_reason": last_finish, "duration_s": round(time.time() - started, 2),
                   "error": str(err), "system": system, "user": user, "response": last_raw})
        raise err


def _usage_dict(u):
    if not u:
        return None
    return {"input": getattr(u, "prompt_tokens", 0) or 0,
            "output": getattr(u, "completion_tokens", 0) or 0}


def _retry_after(err):
    """Honor a server Retry-After (seconds) when present."""
    resp = getattr(err, "response", None)
    if resp is not None:
        val = getattr(resp, "headers", {}).get("retry-after")
        if val and str(val).isdigit():
            return float(val)
    return None

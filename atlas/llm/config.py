"""Model registry — the only place a model or provider is named.

Swapping models is a config edit, never a code change. A new model is a new entry
here; callers refer to models by their friendly key and never learn the provider,
base URL, or wire details.
"""
from __future__ import annotations

PROVIDERS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
    },
}

MODELS = {
    # key                   provider       wire model id                    default effort
    # "serve_via" pins the serving-provider order at the router (fallbacks stay
    # allowed). The same open model differs wildly across serving stacks — in
    # thoroughness and in output-token caps — so pinning keeps runs consistent
    # and avoids low-cap providers that truncate long structured outputs.
    "deepseek-v4-flash": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "reasoning": "high",
                          "serve_via": ["StreamLake", "Fireworks"]},
    "deepseek-v4":       {"provider": "openrouter", "model": "deepseek/deepseek-v4",        "reasoning": "high"},
    "gpt-5":             {"provider": "openrouter", "model": "openai/gpt-5",                "reasoning": "medium"},
    "claude-sonnet":     {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6", "reasoning": None},
}

DEFAULT_MODEL = "deepseek-v4-flash"

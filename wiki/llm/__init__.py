"""L4 — the model provider seam. Swap models via config, never code."""
from .client import LLMClient, Usage
from .config import DEFAULT_MODEL, MODELS
from .mock import MockClient

__all__ = ["LLMClient", "MockClient", "Usage", "MODELS", "DEFAULT_MODEL"]

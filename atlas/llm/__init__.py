"""The model provider seam. Swap models via config, never code."""
from .client import LLMClient, LLMError, Usage
from .config import DEFAULT_MODEL, MODELS

__all__ = ["LLMClient", "LLMError", "Usage", "MODELS", "DEFAULT_MODEL"]

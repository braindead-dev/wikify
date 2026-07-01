"""atlas — a layered pipeline that builds a wiki from a chat.

Layer 1 (extract): granular observations. Later layers compose them into articles.
"""
from .config import ExtractConfig
from .extract import build_observations, extract_all, save_observations
from .observation import TYPES, Observation, observations_schema

__all__ = ["Observation", "TYPES", "observations_schema", "ExtractConfig",
           "extract_all", "build_observations", "save_observations"]

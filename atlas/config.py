"""Config for the pipeline. Simple, clean, generous defaults, max parallelism."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExtractConfig:
    model: str = "deepseek-v4-flash"     # any key in the model registry
    # Generous, granular extraction emits many observations per chunk, so the
    # model's output limit — not its input context — is the binding constraint.
    # Keep chunks small enough that a response never truncates mid-JSON.
    chunk_tokens: int = 80_000           # target input size per chunk
    overlap_tokens: int = 4_000          # overlap between consecutive chunks
    workers: int = 16                    # max parallel extraction calls
    effort: str = "medium"               # reasoning effort for extraction

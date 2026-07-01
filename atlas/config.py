"""Config for the pipeline. Simple, clean, generous defaults, max parallelism."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExtractConfig:
    model: str = "deepseek-v4-flash"     # any key in the model registry
    # Chunk size trades granularity against call count: a smaller window is mined
    # more thoroughly (more observations per message) but takes more calls. Calls
    # run in parallel, so more of them is cheap; just keep a chunk comfortably
    # under the model's output limit so a response never truncates mid-JSON.
    chunk_tokens: int = 80_000           # target input size per chunk
    overlap_tokens: int = 4_000          # overlap between consecutive chunks
    workers: int = 0                     # parallel extraction calls; 0 = all chunks at once
    effort: str = "medium"               # reasoning effort for extraction

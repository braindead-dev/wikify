"""Config for the pipeline. Simple, clean, generous defaults, max parallelism."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExtractConfig:
    model: str = "deepseek-v4-flash"     # any key in the model registry
    # Chunk size trades granularity against call count: a smaller window is mined
    # much more thoroughly (measured: ~5x more observations per message at small
    # sizes), and thorough extraction emits so much JSON that the model's output
    # budget — not its input context — binds. Calls run in parallel, so many small
    # chunks cost little extra wall-clock; keep each chunk's expected output
    # comfortably under the model's limit so a response never truncates mid-JSON.
    chunk_tokens: int = 10_000           # target input size per chunk
    overlap_tokens: int = 1_000          # overlap between consecutive chunks
    workers: int = 0                     # parallel extraction calls; 0 = all chunks at once
    effort: str = "medium"               # reasoning effort: none | low | medium | high
    temperature: float = 0.3             # low = consistent extraction across runs
    # Extraction thoroughness varies run to run even at low temperature: the same
    # chunk can yield 5x fewer observations on a lazy sample. A chunk that lands
    # under this many observations-per-message is re-sampled once and the richer
    # result kept. 0 disables the gate.
    min_density: float = 0.08
    trace: bool = True                   # save each call's prompt+output to traces/ (failures always saved)
    # Requested output ceiling. Reasoning + answer share this budget, and asking
    # for a large one steers OpenRouter to providers that can deliver it instead of
    # low-cap ones that would truncate the JSON. 0 = don't set (use provider default).
    max_tokens: int = 128_000

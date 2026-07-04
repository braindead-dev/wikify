"""Instagram source — reads the official data-export (Download Your Information,
JSON format) unpacked under data/instagram/. DMs, group threads, reactions, and
the photos that ship inside the export."""
from .reader import DEFAULT_ROOT, InstagramExport

__all__ = ["InstagramExport", "DEFAULT_ROOT"]

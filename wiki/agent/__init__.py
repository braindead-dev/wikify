"""The writer agent — scouts capture cited evidence to limbo, a planner promotes
what's matured, curators synthesize deep articles. A map->reduce over a durable
limbo store; both passes are completions (reliable), parallelized across windows
and subjects. See wiki/prompts/{scout,writer}.md and wiki/agent/run.py.
"""
from .run import Context, build_wiki, curate, plan, resolve_identities, scout, windows

__all__ = ["build_wiki", "Context", "windows", "scout", "curate", "plan", "resolve_identities"]

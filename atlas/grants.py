"""Grants — provisioned access to a wiki.

    python3 -m atlas grant salmin --name slackbot --tools context,find,read_page,resolve
    python3 -m atlas mcp salmin --grant <token>
    python3 -m atlas grants [--revoke NAME]

A grant is a capability: WHICH wiki (a scope over the archive), WHICH tools,
until WHEN. The MCP server started with a grant registers only the granted
tools and stamps the grant's name into every audit row — so `atlas log` shows
exactly what each consumer accessed. Because a wiki is compiled from an
explicit source set, granting a wiki can never leak sources outside its scope:
scoping happens at build time, where synthesis can't blend what it never saw.

Grants live in `wikis/grants.json`. Locally they gate honestly (the server
enforces them); when serving moves behind a network boundary the same tokens
become bearer credentials.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path

GRANTS = Path("wikis/grants.json")
ALL_TOOLS = ["overview", "list_pages", "read_page", "search", "find", "related",
             "backlinks", "resolve", "get_image", "context", "correct"]
READ_TOOLS = [t for t in ALL_TOOLS if t != "correct"]


def _load() -> list:
    return json.loads(GRANTS.read_text()) if GRANTS.exists() else []


def _save(grants) -> None:
    GRANTS.parent.mkdir(parents=True, exist_ok=True)
    GRANTS.write_text(json.dumps(grants, indent=1))


def create_grant(wiki, name, tools=None, expires=None, note="") -> dict:
    """Mint a grant. `tools` defaults to read-only (everything but correct);
    `expires` like "90d"/"12h"."""
    tools = [t.strip() for t in (tools or READ_TOOLS) if t.strip() in ALL_TOOLS]
    if not tools:
        raise SystemExit(f"no valid tools — choose from: {', '.join(ALL_TOOLS)}")
    until = ""
    if expires:
        n, unit = int(expires[:-1]), expires[-1]
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        until = (datetime.now() + delta).strftime("%Y-%m-%d %H:%M")
    grants = [g for g in _load() if g["name"] != name]
    grant = {"name": name, "wiki": str(wiki), "tools": tools, "expires": until,
             "note": note, "token": secrets.token_urlsafe(24),
             "created": time.strftime("%Y-%m-%d %H:%M")}
    grants.append(grant)
    _save(grants)
    return grant


def resolve_grant(token) -> dict:
    """The grant for a token — raises on unknown or expired."""
    for g in _load():
        if secrets.compare_digest(g["token"], token):
            if g["expires"] and g["expires"] < time.strftime("%Y-%m-%d %H:%M"):
                raise SystemExit(f"grant '{g['name']}' expired {g['expires']}")
            return g
    raise SystemExit("unknown grant token")


def revoke_grant(name) -> bool:
    grants = _load()
    kept = [g for g in grants if g["name"] != name]
    _save(kept)
    return len(kept) < len(grants)


def list_grants() -> list:
    return _load()

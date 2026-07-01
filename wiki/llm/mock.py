"""A deterministic stand-in for LLMClient — offline tests and reproducible evals.

Same `complete_json` surface as the real client, so nothing above L4 can tell the
difference. Give it a `responder(system, user) -> dict`, or a fixed response.
"""
from __future__ import annotations

from .client import Usage


class MockClient:
    name = "mock"

    def __init__(self, responder=None):
        if responder is None:
            responder = lambda system, user: {"edits": []}
        elif not callable(responder):
            fixed = responder
            responder = lambda system, user: fixed
        self._responder = responder
        self.effort = None
        self.usage = Usage()
        self.calls = []

    def complete_json(self, system: str, user: str, effort=None):
        self.calls.append({"system": system, "user": user})
        return self._responder(system, user)

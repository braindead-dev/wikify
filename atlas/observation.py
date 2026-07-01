"""The Observation — the atomic output of Layer 1 (extract).

Deliberately LOW-LEVEL and granular: an atomic fact or moment that later layers
compose into higher-level articles. "Alice mocked Bob's new haircut, calling it a
bowl cut [#412]", not "Alice is the group's bully". Layer 1 is generous — it emits
any observation that might be wiki-worthy and lets later layers decide.

The wire shape is enforced with genuine structured outputs (see `observations_schema`):
`people` is a strict enum of the chat's participants and every observation must
carry at least one source. `cleaned()` is the second gate — it drops any source id
that isn't a real message and any observation left without one.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

# A non-strict, comprehensive list of example types. The model should prefer these
# for normalization but may coin a new one when nothing fits (so `type` is a free
# string in the schema, not an enum).
TYPES = [
    "notable event",        # a specific thing that happened
    "identity trait",       # something about who a person is
    "backstory",            # a fact about a person's past / life outside the chat
    "relationship",         # dynamic between two people
    "group dynamic",        # a pattern across the whole group
    "inside vocab",         # a coined word / slang term
    "inside joke",          # a running bit / recurring joke
    "quote",                # a memorable line worth preserving verbatim
    "opinion / belief",     # a stance someone takes
    "preference / taste",   # likes/dislikes, music, food, games
    "plan",                 # something the group intends to do
    "milestone",            # a life event (job, move, breakup, etc.)
    "conflict",             # an argument or tension
    "recurring behavior",   # a habit someone repeatedly shows
    "outside character",    # a person mentioned but not in the chat
    "place",                # a location that matters to the group
    "media reference",      # a game/show/song/app the group cares about
]


@dataclass
class Observation:
    title: str                                    # short handle for the observation
    detail: str                                   # the observation, in fuller prose
    type: str                                     # a category (see TYPES; not strict)
    sources: list = field(default_factory=list)   # message row ids backing it (>=1)
    people: list = field(default_factory=list)    # participants involved, by contact name

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Observation":
        return cls(
            title=str(d.get("title", "")).strip(),
            detail=str(d.get("detail", "")).strip(),
            type=str(d.get("type", "")).strip(),
            sources=[int(s) for x in d.get("sources", [])
                     if (s := str(x).strip().lstrip("#")).isdigit()],
            people=[str(p).strip() for p in d.get("people", []) if str(p).strip()],
        )

    def cleaned(self, valid_ids, participants):
        """A validated copy, or None if it can't be salvaged. Keeps only sources
        that are real message ids and people who are real participants, and requires
        a title, a detail, and at least one real source."""
        sources = [s for s in dict.fromkeys(self.sources) if s in valid_ids]
        people = [p for p in dict.fromkeys(self.people) if p in participants]
        if not (self.title and self.detail and sources):
            return None
        return replace(self, sources=sources, people=people)


def observations_schema(participants) -> dict:
    """The genuine structured-output schema for one extraction call. `people` is a
    strict enum of the chat's participants; every observation must have >= 1 source."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["observations"],
        "properties": {
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "detail", "type", "sources", "people"],
                    "properties": {
                        "title": {"type": "string",
                                  "description": "short handle, a few words"},
                        "detail": {"type": "string",
                                   "description": "the observation, concrete and attributed"},
                        "type": {"type": "string",
                                 "description": "category; prefer one of: " + ", ".join(TYPES)},
                        "sources": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "integer"},
                            "description": "message ids backing this — at least one, more is better",
                        },
                        "people": {
                            "type": "array",
                            "items": {"type": "string", "enum": participants},
                            "description": "chat members involved, by exact contact name",
                        },
                    },
                },
            },
        },
    }

"""Name resolution: turn raw handles (phones/emails) into display names.

Resolution order for any handle value:
    1. an explicit alias from identities.json  ("people" map)
    2. the macOS Address Book (Contacts)
    3. the raw value itself (unchanged)

Nothing is ever merged automatically. Two handles only collapse into one
person if *you* list them together under a name in identities.json.
"""
from __future__ import annotations

import glob
import json
import re
import sqlite3
from pathlib import Path


def normalize(value: str) -> str:
    """Canonical key for a handle: last 10 digits of a phone, or a lowercased email."""
    if value and "@" in value:
        return value.lower()
    digits = re.sub(r"[^\d]", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def load_contacts() -> dict:
    """Map normalized handle -> contact name from every Address Book source."""
    base = Path.home() / "Library" / "Application Support" / "AddressBook"
    out: dict = {}
    for db in glob.glob(str(base / "**" / "*.abcddb"), recursive=True):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        cur = con.cursor()
        for table, col in (("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                           ("ZABCDEMAILADDRESS", "ZADDRESS")):
            try:
                cur.execute(
                    f"SELECT r.ZFIRSTNAME, r.ZLASTNAME, r.ZNICKNAME, "
                    f"r.ZORGANIZATION, v.{col} "
                    f"FROM {table} v JOIN ZABCDRECORD r ON v.ZOWNER = r.Z_PK")
            except sqlite3.Error:
                continue
            for first, last, nick, org, value in cur.fetchall():
                name = " ".join(x for x in (first, last) if x) or nick or org
                if value and name:
                    out.setdefault(normalize(value), name)
        con.close()
    return out


def load_identities(path) -> dict:
    """Load an optional identities.json. Returns {} if it's missing."""
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


class Resolver:
    """Resolves handles to names using identities first, then contacts.

    identities schema (all keys optional):
        {
          "me": "Me",                              # how to label yourself
          "people": { "Name": ["+15551234567", "name@example.com"] },
          "groups": { "Group Label": [<chat rowid>, ...] }
        }
    """

    def __init__(self, contacts: dict | None = None, identities: dict | None = None):
        self.contacts = contacts or {}
        identities = identities or {}
        self.me = identities.get("me", "Me")
        self.groups = identities.get("groups", {})
        self._aliases: dict = {}
        for name, handles in identities.get("people", {}).items():
            for handle in handles:
                self._aliases[normalize(handle)] = name

    def name(self, value: str) -> str:
        key = normalize(value)
        return self._aliases.get(key) or self.contacts.get(key) or value

    def sender(self, is_from_me: bool, handle_value: str | None) -> str:
        if is_from_me:
            return self.me
        if not handle_value:
            return "system"
        return self.name(handle_value)

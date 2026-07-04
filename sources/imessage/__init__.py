"""imessage — read and export your local iMessage history.

SDK:
    from imessage import MessagesDB
    with MessagesDB() as db:
        chats = db.chats()                  # faithful: one entry per chat row
        people = db.handles()               # faithful: one entry per endpoint
        msgs = db.messages([512, 638])      # merge chats explicitly, by id
        text = db.export([512, 638], "txt") # transcript string
        data = db.export([512, 638], "json")# structured dict

CLI:
    python3 -m imessage chats
    python3 -m imessage export 512 638 --format txt
"""
from .db import Chat, Handle, Message, MessagesDB
from .identity import Resolver, load_contacts, load_identities

__all__ = ["MessagesDB", "Chat", "Handle", "Message",
           "Resolver", "load_contacts", "load_identities"]

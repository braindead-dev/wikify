"""Ingestion sources — each adapter turns a platform's data into the shared
Message primitive (defined with the richest source, sources.imessage.db).

    sources/imessage/    live macOS Messages database (chats, handles, exports)
    sources/instagram/   official Instagram data-export zip (DMs + photos)

atlas selects chats across sources with qualified ids: a bare number is an
iMessage chat row (`512`), `ig:<thread>` is an Instagram thread. New sources
add a folder here and a prefix there.
"""

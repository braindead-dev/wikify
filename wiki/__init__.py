"""Chat Wiki — a cited, continuously-updated knowledge base over a conversation.

Layers (strict one-way dependency, top depends on bottom):

    reduce/   L3  the reduce(store, chunk) workflow
    llm/      L4  the model provider seam
    store/    L2  the KB store: pages, indexes, the single write path

The `imessage` package is L1 (data access) and is consumed here, never the
reverse. See docs/wiki-agent-design.md for the principles this all follows.
"""

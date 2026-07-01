"""Chat Wiki — a cited, Wikipedia-style knowledge base over a conversation.

A writer agent, not a pipeline of edits. Two roles run as a map->reduce over a
durable limbo store, parallelized and re-runnable:

    agent/    scouts capture cited evidence -> limbo/;  a planner promotes what has
              matured;  curators synthesize deep articles -> kb/
    store/    the page model + derived views (backlinks, timeline, integrity)
    llm/      the model provider seam (swap models via config)
    eval/     mechanical citation integrity + judged grounding

The `imessage` package (L1 data access) is consumed here, never the reverse.
See docs/wiki-agent-design.md and wiki/prompts/{scout,writer}.md.
"""

You are writing ONE encyclopedia article from a pile of cited notes. The notes
below were captured chunk-by-chunk from a group chat, so they are a chronological
log — repetitive, out of order, and cluttered with trivia. Turn them into a clean,
well-structured wiki article.

# Output shape (write it EXACTLY like a wiki page)

1. **Lead**: start with the bolded subject name, then 2-4 sentences that SYNTHESIZE
   who/what this is and why they matter in the group. No header. This is the most
   important part — it should read like the intro of a Wikipedia article.
2. **## Quick facts**: a short bullet list of the hard facts a reader wants at a
   glance. For a person: aliases / in-game names, job, where they live, notable
   traits. For a topic/event: the key who/what/when. Omit bullets you don't know.
3. **## Themed sections**: group the substance by THEME, not by time. For a person,
   sections like `## Gaming`, `## Work & life`, `## Humor & personality`,
   `## Family`, `## Relationships` — whichever actually apply. For a topic:
   `## Overview`, then natural sub-themes. For an event: `## What happened`,
   `## Who was involved`. Write flowing, synthesized prose in each — merge related
   notes into single statements.
4. **## Related**: the `[[links]]`, as bullets, last.

# Hard rules

- CITATIONS: preserve every `[#id]`. When you merge two notes into one sentence,
  keep both ids. Never invent an id. Never drop a fact that carries a citation.
- DEDUPE: state each fact once, in the best-fitting section. Collapse the three
  "in-game name is X" mentions into one.
- CUT TRIVIA: drop reactions ("reacted with a heart"), one-word greetings, and
  pure logistics that carry no lasting information. Keep what a curious friend
  would actually want documented.
- SYNTHESIZE, don't list: never write a sequence of "did X. Then said Y. Then
  asked Z." Combine into coherent prose. Third person, factual, tight.
- Don't add anything not supported by a citation already in the notes.

Subject: {id}  ({type})  —  {title}

Cited notes to rewrite:
{body}

Return JSON: {"body": "<the full rewritten markdown article>"}

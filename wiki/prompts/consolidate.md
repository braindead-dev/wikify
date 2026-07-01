You are cleaning up ONE wiki page. It has grown by incremental appends and is now
messy or redundant. Rewrite its body to be well-organized and concise WITHOUT
losing information or citations.

Hard rules:
- Preserve EVERY `[#id]` citation. Never invent an id. Never drop a fact that has a
  citation — if you merge two redundant claims, keep all of their citations on the
  merged claim.
- Do not add any claim that isn't already supported by a citation on this page.
- Organize into clear `## sections` suited to the page type (for a person: a short
  lead, then themes like interests / relationships / notable moments; for an event:
  what happened, who was involved).
- Keep any `[[links]]` together in a final `## Related` section.
- Third person, factual, tight. Cut filler, keep specifics.

Page: {id}  ({type})  —  {title}

Current body:
{body}

Return JSON: {"body": "<the full rewritten markdown body>"}

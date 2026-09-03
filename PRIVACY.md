# Privacy and data flow

Wikify is designed for records that may contain private conversations, contact
details, photos, voice notes, and information about people who are not the
operator. Treat every input, intermediate artifact, and rendered site as
sensitive unless you have reviewed it.

## What stays local

The source readers access iMessage, Instagram exports, files, and Claude session
exports on the local machine. Face detection and clustering also run locally.
Local state is written beneath `data/`, `wikis/`, and `identities.json`; API
credentials are read from `.env`. These paths are git-ignored, but git ignores
do not encrypt them, remove them from backups, or protect copies placed elsewhere.

## What is sent to model providers

Model-backed `atlas` operations use OpenRouter and the serving provider selected
for the configured model. Depending on the command, requests can include:

- transcript chunks, participant names, and resolved phone/email mappings;
- observations, draft pages, original message excerpts, and corrections;
- normalized copies of images for captioning; and
- normalized audio for transcription.

Provider handling and retention are governed by the providers' current terms and
settings. Review those before processing material you cannot share with a third
party. No model request is made merely by listing or exporting messages with the
source-reader commands.

## Sensitive local artifacts

By default, Atlas keeps full request/response traces for reproducibility. The
archive database, chunks, observations, page drafts, captions, transcripts,
face crops, grants, and audit log can all contain personal data. Grant tokens are
bearer credentials if an MCP server is exposed beyond the local stdio boundary.

## Rendered sites and deployment

Rendered pages are disclosure artifacts, not sanitized summaries. They can
contain names, inferred biographical details, dates, and verbatim message
excerpts with sender and timestamp in citation footnotes. The static site has no
built-in authentication. A deployment can be public even when its source git
repository is private.

Before deploying a private record:

1. Obtain the participants' consent and review the rendered output.
2. Configure authentication or deployment protection at the hosting provider.
3. Check old deployments and stale URLs, not only the current homepage.
4. Search the output for contact details, credentials, addresses, and other
   information that should not be published.
5. Rotate any credential that was ever committed or copied into a trace or site.

`atlas sync` records the files it manages in `.atlas-sync-manifest.json` inside
the deployment repository. Later syncs remove only stale files from that
manifest and preserve unrelated repository files. Sync refuses to run when the
deployment repository already has uncommitted files so it cannot accidentally
include unrelated work in its automated commit.

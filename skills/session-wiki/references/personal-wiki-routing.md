# Personal Wiki Routing

Treat a personal wiki as a separate trust domain. Its own instructions, schema, lifecycle, naming,
timezone, and validators are authoritative.

## Path And Scope

Use a personal-wiki root only when active instructions, the user, or established current context
identifies it. Do not search a home directory for likely wikis. Read only the root instructions,
relevant schema or memory policy, a directly applicable template, and the narrow destination area.

## Portable Trust Model

When the target follows an `inbox -> generated -> reviewed -> canonical` lifecycle:

- `inbox`: AI-writable capture that is not yet verified.
- `generated`: AI-writable synthesis or proposal derived from sources.
- `reviewed`: human-confirmed knowledge; proposal-only for agents.
- `canonical`: long-term policy; explicit human approval required.

Target policy may use different names. Map to the least trusted AI-writable zone and never assume a
higher-trust write is allowed.

## Default Personal Capture

Prefer one concise dated session note in the target's inbox convention. Include:

- context and goal
- durable decisions
- useful patterns or learning
- follow-ups
- risks or unknowns
- sanitized source links or repository aliases
- confidence and review date when the schema supports them

Summarize; do not copy the full conversation or raw command output. A session note is provenance,
not automatically a trusted statement about a project.

## Generated Synthesis

Use a generated evergreen note only when `write=personal-generated`, an existing inbox or reviewed
source set is identified, and target policy allows agent-generated synthesis. Preserve source links,
confidence, conflicts, and review status. Prefer merging with an existing generated topic over
creating duplicates.

## Promotion Hard Stop

Without exact user approval for the target paths and lifecycle action, do not:

- write or move files into reviewed or canonical zones
- change status to reviewed or canonical
- present generated content as human-reviewed
- archive, delete, or move deferred candidates
- update a canonical policy to match an AI inference

An instruction such as "close the session", "default", or "personal capture" is not promotion
approval. Return a promotion candidate with required human checks instead.

## Provenance And Privacy

- Use the target timezone for dates.
- Follow required frontmatter exactly.
- Use durable repository aliases, relative wiki links, commit or PR URLs, or redacted source notes.
- Exclude local absolute paths, secrets, cookies, customer data, raw logs, signed URLs, and private
  company source text.
- Mark uncertain claims with low confidence or the target's verification-needed label.
- Keep external operational facts advisory unless bound to a dated authoritative source.

## Personal Closeout Check

- Path status matches metadata status.
- Required schema fields are present.
- No generated content was promoted.
- No private absolute path or secret value remains.
- Source links are durable and sanitized.
- Required wiki validation passed or the receipt reports the exact blocker.

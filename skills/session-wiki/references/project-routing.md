# Project Documentation Routing

Use repository rules and existing indexes before these generic mappings. This reference helps select
among established destinations; it does not authorize creating all of them.

## Targeted Discovery

1. Read the target repository's active instructions.
2. Read the directly named documentation index, wiki README, or schema.
3. Search for the candidate's domain term and likely destination with focused file searches.
4. Inspect the exact destination section and its source links.
5. Stop discovery when one authoritative destination is clear.

Do not broad-scan unrelated docs, load an entire wiki, or use generated navigation as proof of
content accuracy.

## Generic Mapping

| Candidate | Typical existing destination |
|---|---|
| project purpose, stack, runtime boundary | overview or architecture page |
| directories, modules, ownership, entry points | code map or source map |
| API, event, webhook, external integration | API or integration contract |
| table, important field, migration relationship | schema or data-model page |
| domain invariant, state transition, status code | domain-rules or flow page |
| recurring bug, operational risk, implementation trap | known-issues or troubleshooting page |
| validated local workflow or command constraint | contributor or operations guide |

Repository-specific placement overrides this mapping. Avoid adding transient line numbers when a
stable symbol, module, route, table, or heading identifies the source.

## Update Decision

- `new`: add a compact fact to an existing section.
- `merge`: combine with a related fact to avoid parallel explanations.
- `refresh`: replace a stale statement and preserve relevant compatibility history.
- `conflict`: evidence is insufficient to choose; report both sources and do not guess.
- `duplicate`: make no content change.

Create a new document only when the repository convention clearly requires one and the selected
write scope authorizes it. Otherwise return the proposed title, destination, and source evidence as
a candidate.

## Source Mapping Minimum

A useful source map answers three questions:

1. Where does the behavior enter?
2. Which module or layer owns the rule?
3. Where is persistence, integration, or user-visible output handled?

Record stable paths and symbols, not a complete file inventory. Include tests only when they encode
an important invariant or are the best executable contract.

## Project Closeout Check

- Every edit is supported by a selected-session source.
- Stable project docs contain no task narrative or personal note.
- Existing headings, schemas, and links remain valid.
- Stale facts were replaced or explicitly reported.
- No unrelated dirty file was absorbed into the receipt.

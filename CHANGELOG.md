# Changelog

All notable changes to this repository are documented here.

## [Unreleased]

### Added

- Added the zero-model-call Phase A harness experiment foundation with a preflight-only CLI, host-local-path-free fixed result output, repository validation coverage, and an explicit Python 3.9 CI baseline; live canary and pilot execution remain unavailable.
- Added ten machine-readable Council and Session Wiki scenarios for bare calls, reviewer lifecycle,
  replacement provenance, no-reviewer fallback, seeded-risk recall, strict knowledge review, mixed
  ownership, incompatible writes, and Personal Wiki promotion hard stops.
- Added an extended five-skill checkout for normal live-eval scenarios while preserving the legacy
  three-skill Phase A and harness checkout APIs.

### Changed

- Limited plugin starter prompts to the three supported UI entries while keeping `$council` and
  `$session-wiki` directly discoverable, and declared documentation-write capability explicitly.
- Added manifest contract coverage for starter-prompt limits, Council and Session Wiki exposure,
  and write-capability metadata.
- Hardened Council reviewer lifecycle tracking with separate seats and attempts, canonical target
  provenance, distinct start and completion clocks, terminal-only replacement, and evidence-based
  `complete`, `partial`, and `incomplete` results.
- Preserved explicit target revisions exactly across Council packets, reviewer attempts, delta
  reviews, and final receipts; Council assigns `v0` only when the source has no revision.
- Made Council quality gates independent of token, latency, and model-usage optimization, and
  expanded the release corpus and budget to 36 scenarios, 40 calls, 5,400 seconds, and concurrency
  two.
- Hardened Session Wiki strict review with early scope/write validation, per-claim ownership,
  separate source comparison and publication decisions, and four uniform strict checks.

## [0.4.0] - 2026-08-04

### Added

- Added `$session-wiki` for verified end-of-session knowledge extraction, classification, review,
  project-document routing, and personal-wiki capture.
- Added bare-call option discovery plus quick-candidate, default, personal-capture, and full-closeout
  presets.
- Added source precedence, stable-knowledge eligibility, source-mapping, personal trust-zone, privacy,
  promotion hard-stop, and closeout receipt contracts.
- Added Session Wiki acceptance scenarios, a sample result, contract tests, and Codex UI metadata.

### Changed

- Expanded README installation, usage, and validation guidance for Session Wiki.
- Expanded repository validation to include the Session Wiki skill, references, tests, and sample.
- Bumped the plugin manifest version to `0.4.0`.

## [0.3.0] - 2026-08-04

### Added

- Added `$council` for bounded multi-agent review, ideation, decisions, and artifact refinement.
- Added bare-call option discovery, quick/default/deep presets, a compact review packet, risk-based panel routing, and bounded loop control.
- Added reviewer-failure receipts and explicit `partial`, `incomplete`, `provisional_main_only`, and `static_only` reporting rules for stalls, thread limits, and unavailable mandatory lenses.
- Added Council acceptance scenarios, a sample result, contract tests, and fresh-context forward-test evidence.

### Changed

- Expanded README installation, usage, and validation guidance for Council.
- Expanded repository validation to include the Council skill, references, contract tests, and sample.
- Bumped the plugin manifest version to `0.3.0`.

## [0.2.0] - 2026-07-13

### Added

- Added `$resume-multi-review` for independent recruiter, hiring-manager, and future-teammate resume decisions.
- Added source-authority and freshness rules so public sanitized drafts and generated notes do not override reviewed master resumes.
- Added a strict review output contract with binary decisions, exactly three reasons, one-line fixes, conflict adjudication, evidence-safe rewriting, and bounded repeat loops.
- Added a reusable Korean copy-paste prompt and a source-gap sample review.
- Added acceptance scenarios for authoritative-source selection, reviewer independence, unsupported claim prevention, and loop termination.

### Changed

- Expanded README installation, usage, context discovery, and validation guidance for resume review.
- Expanded repository validation to include the new skill, reference files, and sample.
- Bumped the plugin manifest version to `0.2.0`.

## [0.1.4] - 2026-07-08

### Added

- Added `scripts/validate_repo.sh` for one-command repository validation before public releases.
- Added `docs/forward-test-report.md` to record fresh-context forward-test and clean-install smoke-test evidence.

### Changed

- Documented the validation script in README.
- Bumped the plugin manifest version to `0.1.4`.

## [0.1.3] - 2026-07-08

### Added

- Added `CHANGELOG.md` with public release notes for `0.1.0` through `0.1.3`.
- Added a README link to the changelog.

### Changed

- Refined the adversarial review sample after fresh-context forward-testing to include a HIGH form-submit/persistence finding.
- Bumped the plugin manifest version to `0.1.3`.

## [0.1.2] - 2026-07-08

### Added

- Added an illustrative `adversarial_review` output sample.
- Linked the adversarial review sample from README examples.

### Changed

- Bumped the plugin manifest version to `0.1.2`.

## [0.1.1] - 2026-07-08

### Added

- Added README usage guidance for UI/product workflow artifact approval.
- Added acceptance coverage for read-only or blocked artifact-decision intake.

### Changed

- Clarified `workflow-intake` artifact decisions so read-only or blocked intake uses `create_now: ask` when durable docs are useful.
- Added an output-contract guard against invented combined enum values for autonomy, validation, and E2E decisions.
- Bumped the plugin manifest version to `0.1.1`.

## [0.1.0] - 2026-07-08

### Added

- Added the initial public plugin-ready repository with `workflow`, `workflow-intake`, and `adversarial-review-loop` skills.
- Added README usage examples for workflow routing, AI/LLM eval planning, and adversarial review.
- Added acceptance scenarios for workflow routing, intake, review loop behavior, session conduct, and E2E decisions.
- Added public repository hygiene guidance and validation commands.

[Unreleased]: https://github.com/tomtomjskim/codex-workflow-skills/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/tomtomjskim/codex-workflow-skills/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/tomtomjskim/codex-workflow-skills/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/tomtomjskim/codex-workflow-skills/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.4
[0.1.3]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.3
[0.1.2]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.2
[0.1.1]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.1
[0.1.0]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.0

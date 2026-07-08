# Changelog

All notable changes to this repository are documented here.

## [Unreleased]

### Changed

- Expanded README quick start, skill selection guidance, recommended workflow, and artifact/eval decision documentation.

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

[Unreleased]: https://github.com/tomtomjskim/codex-workflow-skills/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.4
[0.1.3]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.3
[0.1.2]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.2
[0.1.1]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.1
[0.1.0]: https://github.com/tomtomjskim/codex-workflow-skills/releases/tag/v0.1.0

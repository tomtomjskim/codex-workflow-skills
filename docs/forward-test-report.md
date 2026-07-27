# Forward-Test Report

Date: 2026-07-27

This report records the latest validation evidence for the public `codex-workflow-skills` repository. It is intentionally scoped to repeatable checks and known limits, not private session notes.

## Scope

- `workflow-intake` guided-intake behavior for planning, design artifact decisions, validation level selection, and E2E recommendations.
- `adversarial-review-loop` behavior for read-only review routing, evidence requirements, finding severity, disposition, and residual-risk reporting.
- Fixed `current|lean` harness materialization preflight behavior, including path-free summaries and zero model calls.
- Phase A four-task harness experiment preflight behavior, including canonical
  input, masked-review precommit v2, static receipts, deterministic planning,
  cleanup, and fixed path-free result states.
- Repository release hygiene for README links, changelog coverage, plugin manifest version alignment, and public-content scans.

## Fresh-Context Forward Tests

### Workflow Intake Artifact Decisions

Synthetic task: plan a multi-step UI/product workflow where the user had not decided whether planning or design documents should be generated.

Result:

- The first pass exposed an output-contract issue: `artifact_decision.create_now` could incorrectly resolve to `no` even when durable docs were useful but unapproved.
- The skill was updated to require `create_now: ask` for read-only, blocked, or approval-gated intake when durable docs are useful.
- A follow-up pass exposed an enum-shape issue: generated values could combine multiple supported enum values into one invalid value.
- The skill was updated with an output-contract guard requiring exact enum values for autonomy, validation, and E2E decisions.
- Final pass matched the intended contract: planning/design artifact recommendations were separated, approval was requested before durable docs, and E2E was recommended rather than silently skipped.

### Adversarial Review Loop

Synthetic task: review a UI-oriented diff packet with a form submission flow and no full repository context.

Result:

- The reviewer stayed read-only, selected relevant UX/accessibility/QA lenses, and marked unrun checks as `static_only/not_run`.
- The output correctly avoided a false pass when the full diff and executable app were unavailable.
- A material finding was identified: a `button` inside a form without an explicit `type="button"` can submit the form when the command is meant to save a draft.
- The sample adversarial review output was updated to include this HIGH finding and concrete remediation guidance.

## Clean-Install Smoke Test

Expected command sequence:

```bash
git clone https://github.com/tomtomjskim/codex-workflow-skills.git
cd codex-workflow-skills
mkdir -p ~/.codex/skills
ln -s "$PWD/skills/workflow" ~/.codex/skills/workflow
ln -s "$PWD/skills/workflow-intake" ~/.codex/skills/workflow-intake
ln -s "$PWD/skills/adversarial-review-loop" ~/.codex/skills/adversarial-review-loop
test -f ~/.codex/skills/workflow/SKILL.md
test -f ~/.codex/skills/workflow-intake/SKILL.md
test -f ~/.codex/skills/adversarial-review-loop/SKILL.md
./scripts/validate_repo.sh
```

The local smoke test for this release uses an isolated temporary clone and isolated symlink target instead of mutating the user's real `~/.codex/skills` directory.

Latest local result: passed on 2026-07-08 with an isolated clone, isolated skill symlinks, and `./scripts/validate_repo.sh`.

## Known Limits

- Forward tests used synthetic prompts and artifacts rather than a real production repository.
- The clean-install smoke test verifies clone, file visibility, symlink shape, and repository validation. It does not programmatically launch a brand-new Codex UI session and inspect skill-trigger behavior.
- Browser or Playwright E2E remains task-dependent. `workflow-intake` should recommend it by default for real UI work, but these skills themselves do not include a browser app to exercise.
- Phase A task qualification remains `operator_attested_static`, and masked
  review remains `operator_attested_aggregated_review`. The checks do not
  independently execute task validators or prove actual reviewer delivery,
  observation of the delivered raw bytes, reviewer authentication or
  independence, aggregation replay, crash-durable/external anchoring, or
  elimination of custodian collusion, multiple-seed/grinding, same-UID, and
  filesystem-metadata residual risks. The content-addressed artifact manifest
  is an operator-attested commitment to declared content and source bindings,
  not proof that a reviewer received or observed those bytes. A Phase B live
  packager must hash the actual packaged bytes and compare them with the
  declared digests before delivery.

## Release Gate

### Legacy live-eval runner validation

- Deterministic scenario, isolation, checkout, budget, artifact, and runner tests are part of the non-network repository release gate.
- The runner dry-run verifies planning preflight only and reports `preflight_only` with zero model calls.
- The separate harness materialization preflight verifies fixed `current|lean` bundle materialization, clean-HEAD skill routing hashes, and the sealed temporary home. A pass reports `harness_preflight_only`, while actual Codex loading and model conformance remain `not_run` with zero model calls.
- Fixture-backed CLI smoke on 2026-07-23 passed for both profiles with `materialization_result=pass`, `model_conformance=not_run`, and `model_calls=0`; the profile `agents_hash` values differed while the bundle and shared routing hashes matched.
- live model execution: not_run
- No model-quality or production-network conclusion is inferred from deterministic tests or dry-run output.

### Phase A experiment preflight validation

The following local checks passed on 2026-07-27 with Python 3.9.6:

```bash
python3 -m unittest tests.test_live_eval_experiment tests.test_repository_validation -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
python3 -m unittest tests.test_live_eval_experiment_plan -v
python3 -m unittest tests.test_live_eval_experiment_receipts -v
python3 -m unittest tests.test_repository_validation -v
./scripts/validate_repo.sh
```

- Task 10 masked-review v2 focused plan tests: 56 passed.
- Task 10 masked-review v2 focused receipt/replay tests: 55 passed.
- A-01 repository validation: 11 passed.
- The current focused integration of those three suites: 122 passed.
- Full live-eval-pattern discovery: 599 passed.
- Full repository validation: 749 tests ran, with 2 environment-dependent
  shared-agent checks skipped because `SHARED_AGENTS_ROOT` was not configured;
  the validation gate passed.
- The v2 tests verified exact seed/context commitment binding, pre-unmask seed
  secrecy, context-bound HMAC order and neutral IDs, salted mapping
  commitment, schema-2 packet binding, seed-HMAC hiding commitments over
  content-addressed review-artifact semantics, rejection of stale context,
  alternate seed/order/mapping, pre-unmask enumeration, and swapped, stale, or
  arbitrary self-consistently rehashed artifact commitments, plus same-task
  post-unmask
  lean-HIGH/current-not-HIGH adjudication. These are deterministic contract
  tests, not evidence that a live masked review occurred or that declared
  bytes were actually delivered to a reviewer.
- The fixture-backed CLI preflight exited `0` with `status=static_only`, `live_backend_state=live_backend_not_implemented`, `global_agents_marker_state=global_agents_marker_not_run`, `pilot_state=pilot_not_run`, `qualification_evidence_classification=operator_attested_static`, `materialization_result=verified`, `cleanup_state=removed`, and `model_calls=0`.
- The owned `phase-a` tree was removed and the caller-owned temporary parent remained empty.
- Captured stdout and stderr contained none of the input, bundle, skill, task-source, or temporary absolute paths and none of the private task sentinel bytes. Invalid live-intent arguments produced one fixed blocked JSON result, exit `2`, and empty stderr before file reading or orchestration.
- The dependency gate covered all six Phase A modules and rejected representative aliased, dynamic, relative, authentication, network, environment-credential, checkout, harness, and subprocess escapes. This AST policy is a regression gate, not proof against intentionally obfuscated reflection; runtime poison-pill tests remain part of the boundary.
- The workflow is configured with `actions/setup-python@v7`, `python-version: "3.9"`, and an explicit 3.9 assertion. No remote GitHub Actions result is claimed here.
- live containment backend, canary, pilot, approval, credential, executable, network, live-ledger, and paid model calls: unavailable or not_run
- Phase A remains `static_only`; model, API, and network calls were 0. No live
  experiment, model-quality, harness-consumption, profile-winner, reviewer
  independence, or production-containment conclusion is inferred.
- The current final decision projection omits
  `review_evidence_classification`; Phase B output must carry
  `operator_attested_aggregated_review` explicitly.

Before public release, run:

```bash
./scripts/validate_repo.sh
```

For higher-risk skill edits, add at least one fresh-context forward test against `tests/acceptance-scenarios.md` and update this report with the result and limits.

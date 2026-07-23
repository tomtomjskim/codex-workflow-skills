# Harness Variant Preflight Implementation Plan

> **Execution note:** implement each task test-first, preserve the legacy live-eval contracts, and stop before any paid model call.

**Goal:** Add a deterministic, zero-model-call preflight that proves a fixed `current|lean` harness bundle can be materialized into an isolated Codex home together with the exact clean-HEAD workflow skills.

**Design source:** The approved research and experiment design in the personal wiki, plus independent local runner audit and adversarial review. This plan implements experiment plumbing only. It does not select a winning harness, edit global configuration, or claim model-quality evidence.

**Architecture:** Keep `run_eval()` and its legacy dry-run/live JSON contract unchanged. Add a separate harness module that reads only a fixed bundle inventory, safely snapshots `AGENTS.md` and shared role files, rewrites the one expected absolute role include to an isolated-home include, installs the existing three clean-HEAD skills, seals the resulting home, and returns path-free digests. Git object snapshot subprocesses are allowed only through a filtered environment and disabled system/global config, fsmonitor, hooks, and recursive submodules; Codex/model subprocesses remain forbidden. Expose the core through an all-or-none CLI option group that is valid only with `--dry-run`; the result must always report `model_calls=0` and `model_conformance=not_run`.

**Technology:** Python standard library, existing `scripts.live_eval.checkout` Git-object materializer, existing isolation seal, `unittest`, repository validation script.

## Fixed contracts and non-goals

- Bundle inventory is fixed:

  ```text
  harness.json
  profiles/current/AGENTS.md
  profiles/lean/AGENTS.md
  shared/agents/*.toml
  shared/common-agents/*.md
  ```

- `harness.json` has only `schema_version: 1` and a `bundle_id` matching `[a-z0-9][a-z0-9._-]{0,63}`. File paths are never supplied by manifest data.
- Profile is an enum: `current` or `lean`; it is never interpolated from arbitrary input.
- Adapter and common-role stems must both equal the exact 16-role allowlist: `accessibility-reviewer`, `api-reviewer`, `architect`, `code-reviewer`, `dba`, `designer`, `developer`, `documenter`, `explorer`, `performance-reviewer`, `pm`, `publisher`, `qa-engineer`, `security-reviewer`, `test-coverage-reviewer`, and `ux-reviewer`.
- Both profiles share the same role files, and their canonical `AGENTS.md` byte hashes must differ. Static verification does not prove that the policy meanings differ.
- Every adapter must contain exactly one expected absolute include for its same-stem common role; materialization rewrites that include to `~/.agents/common-agents/<role>.md`.
- Materialized `CODEX_HOME` has exactly `.agents`, `.live-eval-checkout.json`, `AGENTS.md`, `agents`, and `skills`.
- The legacy verifier continues to require exactly `.live-eval-checkout.json` and `skills`.
- Output contains identifiers, counts, and `sha256:` digests only. A path-free canonical `bundle_digest` binds every fixed relative file path, mode, and byte sequence independently of `bundle_id`. Output contains no source paths or source text.
- Static preflight success does not prove that Codex consumed the files or that either harness performs better.
- No live Codex process, API key read, network call, dependency addition, global-file mutation, reviewed-wiki promotion, or paid model call is in scope.

## Task 1: Safe fixed-inventory harness materialization

**Files:**

- Create: `scripts/live_eval/harness.py`
- Create: `tests/test_live_eval_harness.py`
- Modify: `scripts/live_eval/checkout.py`
- Modify: `tests/test_live_eval_checkout.py`

### Step 1: Write failing contract tests

Cover at minimum:

- both profiles materialize the fixed home inventory and have different `AGENTS.md` hashes;
- shared adapter/common-role hashes and skill-routing hashes are stable across profiles;
- the one adapter include is rewritten to the isolated-home include;
- missing, duplicate, or wrong-role includes are rejected;
- extra/missing/casefold/Unicode-aliased inventory, symlinks, special files, hardlinks, unsafe modes, and oversized files are rejected;
- materialized content, mode, link, or extra-entry tampering blocks verification;
- public legacy checkout verification still rejects harness-only entries;
- public legacy checkout API and manifest schema stay unchanged;
- returned manifests and exceptions do not expose absolute source paths or source contents;
- matched role-pair deletion/addition, invalid bundle IDs, identical profile hashes, and same-ID/different-content bundle digests are covered;
- malicious local Git fsmonitor configuration is disabled, parent secrets are not forwarded, and materialized checkout hardlinks are rejected;

Run:

```bash
python3 -m unittest tests.test_live_eval_harness tests.test_live_eval_checkout -v
```

Expected: fail because the harness module and fixed harness verifier do not exist.

### Step 2: Implement the minimal fixed policy

- Add a private checkout verifier that accepts an internally supplied exact inventory.
- Keep `verify_loaded_checkout()` pinned to the legacy inventory.
- In `harness.py`, validate the private bundle root and fixed tree without following links.
- Read bounded regular files through no-follow descriptors, rejecting hardlinks and detected source mutation.
- Copy to a private empty home using exclusive creation and read-only modes.
- Reuse `install_checkout_skills()` and `seal_codex_home()`.
- Compute a canonical skill-routing digest from plugin hash, skill names, and materialized skill hashes.
- Return immutable source/materialized manifests containing no `Path` fields.
- Fail closed with sanitized reason codes.
- Run Git with a minimal environment, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, and command-local disabling of fsmonitor, hooks, and recursive submodules.
- Bind the source with `bundle_digest`, require different current/lean agent hashes, and pin the exact 16-role inventory.
- Compare harness parent/home identities around materialization and verification. Full fd-based seal walking and defense against an active same-UID process replacing a parent directory during the final seal are explicitly out of scope; the boundary is a private `0700` owned temporary home followed by immediate verification.

### Step 3: Run focused tests and commit

```bash
python3 -m unittest tests.test_live_eval_harness tests.test_live_eval_checkout -v
git diff --check
git add scripts/live_eval/harness.py scripts/live_eval/checkout.py tests/test_live_eval_harness.py tests/test_live_eval_checkout.py docs/superpowers/plans/2026-07-23-harness-variant-preflight-plan.md
git commit -m "feat(eval): materialize fixed harness variants"
```

## Task 2: Zero-call runner and CLI integration

**Files:**

- Modify: `scripts/run_live_eval.py`
- Modify: `tests/test_live_eval_runner.py`
- Modify: `scripts/validate_repo.sh`
- Modify: `README.md`
- Modify: `docs/forward-test-report.md`

### Step 1: Write failing runner tests

Cover at minimum:

- `--harness-profile`, `--harness-bundle`, and `--variant-repo` are all-or-none and require `--dry-run`;
- harness dry-run materializes and verifies either profile without reading the API key, resolving/probing Codex, invoking a Codex/model subprocess, or making a model call; hardened Git object snapshot subprocesses are permitted;
- output uses a separate `harness_preflight_only` contract with `materialization_result=pass`, `model_conformance=not_run`, and `model_calls=0`;
- mismatch/tamper returns a blocked result, never pass;
- output includes hashes but no absolute source paths;
- the existing dry-run and live request/result schemas remain unchanged.

Run:

```bash
python3 -m unittest tests.test_live_eval_runner -v
```

Expected: fail because harness CLI routing does not exist.

### Step 2: Implement the separate preflight branch

- Add immutable `HarnessDryRunRequest` and `HarnessDryRunResult` types.
- Keep the Task 1 harness core independent of scenario selection. At the CLI boundary, compose it with the existing unchanged `EvalRequest` dry-run selection, then materialize and verify one harness in an owned private temporary home.
- Route the harness CLI branch before auth lookup, Codex executable lookup, capability probe, or invocation.
- Ensure success and blocked output serialize only the separate sanitized contract.
- Clean up only the temporary paths owned by this invocation; preserve replacements and report a sanitized residual-state flag when cleanup cannot be proven.
- Require the new module and test file in `scripts/validate_repo.sh`.
- Document the distinction among planning-only dry-run, harness materialization preflight, and actual model conformance.

### Step 3: Run focused and full validation

```bash
python3 -m unittest tests.test_live_eval_runner -v
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
./scripts/validate_repo.sh
git diff --check
```

Expected: all pass; repository validation still reports external shared-agent audit as `not_run` unless explicitly configured, and no model call occurs.

### Step 4: Run a fixture-backed CLI smoke test and commit

Run both profiles against a private test bundle and capture only the sanitized JSON summaries. Confirm `model_calls=0` and `model_conformance=not_run` for both.

```bash
git add scripts/run_live_eval.py tests/test_live_eval_runner.py scripts/validate_repo.sh README.md docs/forward-test-report.md
git commit -m "feat(eval): add zero-call harness preflight"
```

## Task 3: Independent review, wiki evidence, and bounded commits

**Repositories:**

- Review feature branch: `codex-workflow-skills`
- Update staged research evidence: `personal-wiki`

### Step 1: Independent adversarial review

Review the branch against this plan, focusing on:

- any path by which the new dry-run can read credentials or invoke Codex;
- path traversal, symlink/hardlink, race, inventory, digest, and cleanup failures;
- accidental weakening of the legacy exact-inventory verifier;
- output leakage or overclaiming static evidence as model evidence;
- unnecessary framework complexity.

Fix only accepted findings, rerun the smallest failing test first, then the full repository validator.

### Step 2: Record actual—not intended—evidence in personal wiki

Update the research `README.md`, `experiment-plan.md`, or `review-log.md` only with verified facts: branch/commit identifiers, exact validation commands, pass/fail status, fixture smoke-test summaries, `model_calls=0`, and the remaining cost-cap/live-evaluation gate. Do not promote the research to `reviewed`.

Regenerate and validate:

```bash
/usr/local/bin/python3.12 -B scripts/scan_knowledge_extension_candidates.py --date 2026-07-20 --wiki wiki --review-pack wiki/generated/llm/knowledge-systems/review-pack-2026-07-20.md
/usr/local/bin/python3.12 -B scripts/generate_wiki_graph.py
/usr/local/bin/python3.12 -B scripts/validate_generated_artifacts.py --date 2026-07-20
/usr/local/bin/python3.12 -B scripts/validate_wiki.py
git diff --cached --check
```

### Step 3: Commit only the approved documentation scope

Review the cached diff and commit the four research documents plus the four regenerated artifacts. Do not push either repository and do not change global rules.

## Completion evidence

Completion for this approved step requires:

- feature worktree clean after its commits;
- focused tests and `./scripts/validate_repo.sh` passing from the feature worktree;
- fixture-backed current and lean harness preflights both reporting zero model calls;
- fixed current and lean `AGENTS.md` byte digests are unequal, while identical `bundle_id` values remain distinguishable by `bundle_digest`;
- personal-wiki generated artifacts fresh and wiki validation passing;
- no global config/rule mutation, no live model execution, no reviewed promotion, and no push;
- any residual risk and the numeric cost-cap decision reported explicitly.

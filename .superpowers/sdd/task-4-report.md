# Task 4 Report: Coordination Validation and Receipts

## Status

Implemented path, DAG, contract-ledger, receipt, and handoff validation for Task 4.

## Ambiguity resolution

The brief's four-argument function did not identify the sources for trigger
policy, checkout tree hash, run ID, or time. The coordination lead resolved this
by retaining the four positional arguments and adding keyword-only injection
points:

```python
validate_coordination(
    repo_root,
    manifest,
    inventory,
    contract,
    *,
    trigger_matrix=None,
    checkout_tree_hash=None,
    run_id_factory=None,
    clock=None,
)
```

Missing or incompatible policy fails closed. The default checkout hash is read
with `git rev-parse HEAD^{tree}`; UUID and UTC time have injectable factories.
Contract shape and hash domains follow the canonical design document.

## RED evidence

Initial command:

```text
python3 -m unittest tests.test_coordination_validation tests.test_handoff_validation -v
```

Initial output (exit 1):

```text
test_coordination_validation (unittest.loader._FailedTest) ... ERROR
test_handoff_validation (unittest.loader._FailedTest) ... ERROR
ModuleNotFoundError: No module named 'scripts.workflow_coordination.validate'
ModuleNotFoundError: No module named 'scripts.workflow_coordination.receipts'
Ran 2 tests in 0.000s
FAILED (errors=2)
```

Self-review boundary RED command:

```text
python3 -m unittest tests.test_coordination_validation.CoordinationValidationTests.test_rejects_broken_ledger_chain_and_hash_domain tests.test_coordination_validation.CoordinationValidationTests.test_rejects_unfrozen_contracted_contract -v
```

Output (exit 1):

```text
test_rejects_broken_ledger_chain_and_hash_domain ... ERROR
ValidationError: ledger chain mismatch at entry 0
test_rejects_unfrozen_contracted_contract ... FAIL
AssertionError: ValidationError not raised
Ran 2 tests in 0.006s
FAILED (failures=1, errors=1)
```

Receipt profile binding RED command:

```text
python3 -m unittest tests.test_coordination_validation.CoordinationValidationTests.test_rejects_submitted_required_sets_and_returns_bound_receipt -v
```

Output (exit 1):

```text
AttributeError: 'ValidationReceipt' object has no attribute 'derived_profiles'
Ran 1 test in 0.005s
FAILED (errors=1)
```

Canonical error wrapping RED command:

```text
python3 -m unittest tests.test_coordination_validation.CoordinationValidationTests.test_wraps_noncanonical_artifact_as_validation_error -v
```

Output (exit 1):

```text
CanonicalJSONError: floating-point values are not allowed
Ran 1 test in 0.004s
FAILED (errors=1)
```

## GREEN evidence

Focused command:

```text
python3 -m unittest tests.test_coordination_validation tests.test_handoff_validation -v
```

Output (exit 0):

```text
Ran 10 tests in 0.029s
OK
```

Full unit command:

```text
python3 -m unittest discover -v
```

Output (exit 0):

```text
Ran 32 tests in 0.038s
OK
```

Repository validation command:

```text
./scripts/validate_repo.sh
```

Output (exit 0):

```text
Skill is valid! (workflow)
Skill is valid! (workflow-intake)
Skill is valid! (adversarial-review-loop)
Plugin validation passed
ok: repository validation passed
```

## Files

- `scripts/workflow_coordination/validate.py`
- `scripts/workflow_coordination/receipts.py`
- `tests/test_coordination_validation.py`
- `tests/test_handoff_validation.py`

## Coverage and design notes

- Rejects absolute paths, parent traversal, glob syntax, non-POSIX separators,
  repository-root ownership, symlink/root escape, duplicate owners, duplicate
  paths, and ancestor overlap.
- Rejects missing dependency references and dependency cycles.
- Re-derives route and required sets; submitted route/sets cannot override it.
- Contract core hash covers only `contract_core`; every ledger entry binds the
  current core, canonical previous-entry hash, and its own canonical body.
  `ledger_hash` covers the ordered list of entry hashes.
- Contracted routes require a current frozen contract. Missing/incompatible
  derivation inputs fail closed.
- Receipt binds artifact hashes, optional core hash, checkout tree, derived
  route/profile/required sets, normalized ownership, run ID, and UTC time.
- Handoff validation accepts tracked or untracked descendant paths only under
  the selected workstream's normalized ownership.

## Concerns

- Closure semantics beyond ordered ledger integrity (review completion,
  acknowledgements, checkpoints, and integration closure) remain intentionally
  outside Task 4.
- Runtime/cryptographic producer identity and hooks remain out of scope.

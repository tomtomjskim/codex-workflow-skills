# Coordination CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dependency-free CLI that prepares canonical coordination artifacts, derives parallel routes and required sets, validates contracts and handoffs, and emits machine-verifiable receipts.

**Architecture:** A Python 3.9 package under `scripts/workflow_coordination/` owns canonical JSON, plan projection, derivation, validation, and receipt generation. A thin executable dispatches `prepare-coordination`, `validate-coordination`, and `validate-handoff`; workflow skills consume its JSON output and fall back to sequential work when validation is unavailable.

**Tech Stack:** Python 3.9 standard library, `unittest`, SHA-256, Git CLI, Bash release validation.

## Global Constraints

- No production dependency or PyYAML dependency.
- Canonical machine inputs are UTF-8 JSON only.
- Runtime hooks and cryptographic runtime identities remain out of scope.
- Missing or incompatible validation must produce sequential single-owner fallback.
- Test-first red-green implementation is required for every behavior.
- Source design: `docs/superpowers/specs/2026-07-21-risk-based-agent-workflow-validation-design.md`.

---

### Task 1: Canonical JSON and Hash Domains

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/workflow_coordination/__init__.py`
- Create: `scripts/workflow_coordination/canonical_json.py`
- Create: `tests/__init__.py`
- Create: `tests/test_canonical_json.py`

**Interfaces:**
- Consumes: UTF-8 JSON bytes or Python JSON-compatible values.
- Produces: `load_canonical_input(data: bytes) -> object`, `canonical_bytes(value: object) -> bytes`, `sha256_id(value: object) -> str`.

- [ ] **Step 1: Write failing canonicalization tests**

```python
import unittest

from scripts.workflow_coordination.canonical_json import (
    CanonicalJSONError,
    canonical_bytes,
    load_canonical_input,
    sha256_id,
)


class CanonicalJSONTests(unittest.TestCase):
    def test_key_order_and_whitespace_have_same_hash(self):
        left = load_canonical_input(b'{"b":2, "a":1}')
        right = load_canonical_input(b'{\n"a":1,"b":2\n}')
        self.assertEqual(canonical_bytes(left), b'{"a":1,"b":2}')
        self.assertEqual(sha256_id(left), sha256_id(right))

    def test_rejects_duplicate_keys(self):
        with self.assertRaisesRegex(CanonicalJSONError, "duplicate key: a"):
            load_canonical_input(b'{"a":1,"a":2}')

    def test_rejects_floats_and_non_nfc_strings(self):
        with self.assertRaisesRegex(CanonicalJSONError, "floating-point"):
            load_canonical_input(b'{"value":1.5}')
        with self.assertRaisesRegex(CanonicalJSONError, "Unicode NFC"):
            canonical_bytes({"value": "e\u0301"})
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_canonical_json -v`

Expected: import failure for `scripts.workflow_coordination.canonical_json`.

- [ ] **Step 3: Implement canonical JSON**

```python
class CanonicalJSONError(ValueError):
    pass


def load_canonical_input(data: bytes) -> object:
    """Parse JSON with duplicate-key, float, and non-finite-number rejection."""


def canonical_bytes(value: object) -> bytes:
    """Validate NFC strings and encode sorted compact UTF-8 JSON."""


def sha256_id(value: object) -> str:
    """Return sha256:<lowercase hex> over canonical_bytes(value)."""
```

Implementation must use `object_pairs_hook` for duplicate rejection,
`parse_float`/`parse_constant` rejection, recursive integer/bool/null validation,
`unicodedata.is_normalized("NFC", value)`, and `json.dumps(sort_keys=True,
separators=(",", ":"), ensure_ascii=False, allow_nan=False)`.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m unittest tests.test_canonical_json -v`

Expected: all canonical JSON tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/workflow_coordination tests/test_canonical_json.py
git commit -m "feat: add canonical coordination JSON"
```

### Task 2: Prepare Manifest and Independent Inventory

**Files:**
- Create: `scripts/workflow_coordination/prepare.py`
- Create: `tests/test_prepare_coordination.py`
- Create: `tests/fixtures/coordination/approved-plan.json`

**Interfaces:**
- Consumes: canonical approved plan JSON and optional repository interface catalog.
- Produces: `prepare_coordination(plan, catalog) -> PreparedCoordination` containing `manifest`, `inventory`, and both hashes.

- [ ] **Step 1: Write failing projection tests**

```python
class PrepareCoordinationTests(unittest.TestCase):
    def test_generates_manifest_and_inventory_from_one_plan(self):
        prepared = prepare_coordination(APPROVED_PLAN, API_CATALOG)
        self.assertEqual(prepared.manifest["workstreams"][0]["id"], "frontend")
        self.assertIn("settings-v1", prepared.inventory["known_interface_ids"])
        self.assertTrue(prepared.manifest_hash.startswith("sha256:"))
        self.assertTrue(prepared.inventory_hash.startswith("sha256:"))

    def test_rejects_duplicate_workstream_ids(self):
        plan = dict(APPROVED_PLAN)
        plan["workstreams"] = [APPROVED_PLAN["workstreams"][0]] * 2
        with self.assertRaisesRegex(PreparationError, "duplicate workstream id"):
            prepare_coordination(plan, API_CATALOG)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_prepare_coordination -v`

Expected: import failure for `prepare_coordination`.

- [ ] **Step 3: Implement plan projection**

```python
@dataclass(frozen=True)
class PreparedCoordination:
    manifest: dict
    inventory: dict
    manifest_hash: str
    inventory_hash: str


from typing import Optional


def prepare_coordination(plan: dict, catalog: Optional[dict]) -> PreparedCoordination:
    """Project one approved plan into canonical manifest and independent inventory."""
```

The inventory must derive expected workstreams and changed surfaces from the
approved plan, merge known interface IDs from the repository catalog, and never
copy a user-supplied `selected_route` or required reviewer set.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m unittest tests.test_prepare_coordination -v`

Expected: projection and duplicate-ID tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/workflow_coordination/prepare.py tests/test_prepare_coordination.py tests/fixtures/coordination/approved-plan.json
git commit -m "feat: prepare coordination artifacts"
```

### Task 3: Derive Route, Required Sets, and Contract Core

**Files:**
- Create: `scripts/workflow_coordination/derive.py`
- Create: `scripts/workflow_coordination/models.py`
- Create: `tests/test_coordination_derivation.py`

**Interfaces:**
- Consumes: prepared manifest, inventory, canonical reviewer trigger matrix.
- Produces: `derive_coordination(...) -> DerivedCoordination` with completeness, profiles, consumers, handoffs, acknowledgements, and reviewers.

- [ ] **Step 1: Write failing derivation tests**

```python
class DerivationTests(unittest.TestCase):
    def test_derives_shared_api_and_reviewers(self):
        result = derive_coordination(MANIFEST, INVENTORY, TRIGGER_MATRIX)
        self.assertEqual(result.route, "contracted")
        self.assertTrue(result.profiles.shared_interface)
        self.assertEqual(result.affected_consumers, ("frontend",))
        self.assertIn("api-reviewer", result.required_reviewers)

    def test_inventory_mismatch_blocks(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["workstreams"][0]["consumes"] = []
        result = derive_coordination(manifest, INVENTORY, TRIGGER_MATRIX)
        self.assertEqual(result.completeness, "mismatch")
        self.assertEqual(result.route, "blocked")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_coordination_derivation -v`

Expected: import failure for derivation types.

- [ ] **Step 3: Implement deterministic derivation**

```python
@dataclass(frozen=True)
class DerivedCoordination:
    completeness: str
    route: str
    profiles: DerivedProfiles
    affected_consumers: tuple[str, ...]
    required_handoffs: tuple[tuple[str, str], ...]
    required_acknowledgements: tuple[str, ...]
    required_reviewers: tuple[str, ...]


def derive_coordination(manifest: dict, inventory: dict, trigger_matrix: dict) -> DerivedCoordination:
    """Derive all gate inputs; never accept submitted route or required sets."""
```

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m unittest tests.test_coordination_derivation -v`

Expected: shared-interface and mismatch cases pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/workflow_coordination/derive.py scripts/workflow_coordination/models.py tests/test_coordination_derivation.py
git commit -m "feat: derive coordination requirements"
```

### Task 4: Validate Paths, DAG, Contract Ledger, and Handoffs

**Files:**
- Create: `scripts/workflow_coordination/validate.py`
- Create: `scripts/workflow_coordination/receipts.py`
- Create: `tests/test_coordination_validation.py`
- Create: `tests/test_handoff_validation.py`

**Interfaces:**
- Consumes: repo root, manifest, inventory, optional contract core and ledger, base tree hash.
- Produces: `ValidationReceipt` JSON or structured validation errors; `validate_handoff(...)` for actual changed paths.

- [ ] **Step 1: Write failing negative tests**

```python
class CoordinationValidationTests(unittest.TestCase):
    def test_rejects_cycle_and_ancestor_path_overlap(self):
        with self.assertRaisesRegex(ValidationError, "dependency cycle"):
            validate_coordination(REPO_ROOT, CYCLIC_MANIFEST, INVENTORY, None)
        with self.assertRaisesRegex(ValidationError, "path overlap"):
            validate_coordination(REPO_ROOT, OVERLAP_MANIFEST, INVENTORY, None)

    def test_rejects_stale_ledger_entry(self):
        with self.assertRaisesRegex(ValidationError, "contract core hash mismatch"):
            validate_coordination(REPO_ROOT, MANIFEST, INVENTORY, STALE_CONTRACT)

    def test_handoff_rejects_cross_workstream_write(self):
        with self.assertRaisesRegex(ValidationError, "outside owned paths"):
            validate_handoff(REPO_ROOT, RECEIPT, "frontend", ["src/api/settings.py"])
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_coordination_validation tests.test_handoff_validation -v`

Expected: import failure for validator functions.

- [ ] **Step 3: Implement validation and receipts**

```python
from typing import Optional


@dataclass(frozen=True)
class ValidationReceipt:
    schema_version: int
    manifest_hash: str
    inventory_hash: str
    contract_core_hash: Optional[str]
    checkout_tree_hash: str
    derived_route: str
    required_sets: dict
    normalized_paths: dict
    run_id: str
    recorded_at: str


def validate_coordination(repo_root: Path, manifest: dict, inventory: dict, contract: Optional[dict]) -> ValidationReceipt:
    """Validate DAG, canonical paths, completeness, hash domains, and derived sets."""


def validate_handoff(repo_root: Path, receipt: ValidationReceipt, workstream_id: str, changed_paths: list[str]) -> None:
    """Reject tracked or untracked changes outside derived ownership."""
```

For the first release, reject glob metacharacters, absolute paths, `..`,
nonexistent parents outside root, duplicate owners, cycles, stale core hashes,
and submitted required sets that differ from derivation.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m unittest tests.test_coordination_validation tests.test_handoff_validation -v`

Expected: all negative cases fail closed and valid fixtures pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/workflow_coordination/validate.py scripts/workflow_coordination/receipts.py tests/test_coordination_validation.py tests/test_handoff_validation.py
git commit -m "feat: validate coordination and handoffs"
```

### Task 5: CLI, Skill Contracts, and Release Gate

**Files:**
- Create: `scripts/workflow`
- Create: `scripts/validate_policy_contracts.py`
- Create: `tests/test_workflow_cli.py`
- Create: `tests/test_policy_contracts.py`
- Create: `skills/workflow-intake/references/parallel-coordination.md`
- Modify: `skills/workflow-intake/SKILL.md`
- Modify: `skills/workflow-intake/references/session-conduct.md`
- Modify: `skills/workflow-intake/references/review-packet.md`
- Modify: `skills/adversarial-review-loop/references/reviewer-trigger-matrix.md`
- Modify: `scripts/validate_repo.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 1-4 modules.
- Produces: `workflow prepare-coordination`, `workflow validate-coordination`, and `workflow validate-handoff` JSON commands plus skill routing rules.

- [ ] **Step 1: Write failing policy-contract tests**

```python
class PolicyContractTests(unittest.TestCase):
    def test_unsupported_high_sample_is_rejected(self):
        errors = validate_review_sample(UNSUPPORTED_HIGH_SAMPLE)
        self.assertIn("HIGH requires direct evidence or needs-investigation", errors)

    def test_hygiene_scans_every_tracked_text_file(self):
        errors = scan_tracked_text_files(REPO_WITH_PRIVATE_ROOT_FILE)
        self.assertIn("private path", errors[0])
```

- [ ] **Step 2: Run policy tests and verify RED**

Run: `python3 -m unittest tests.test_policy_contracts -v`

Expected: policy validator import failure.

- [ ] **Step 3: Implement dependency-free policy checks**

```python
def validate_review_sample(sample: dict) -> list[str]:
    """Reject unsupported severity, invalid enums, and weak verification claims."""


def scan_tracked_text_files(repo_root: Path) -> list[str]:
    """Use git ls-files -z, skip binary files, and scan every tracked text file."""
```

- [ ] **Step 4: Write failing CLI tests**

```python
class WorkflowCLITests(unittest.TestCase):
    def test_prepare_and_validate_emit_json(self):
        prepared = run_cli("prepare-coordination", PLAN_FIXTURE)
        self.assertEqual(prepared.returncode, 0)
        self.assertIn("manifest_hash", json.loads(prepared.stdout))
        validated = run_cli("validate-coordination", prepared_output=prepared.stdout)
        self.assertEqual(validated.returncode, 0)
        self.assertIn("run_id", json.loads(validated.stdout))

    def test_missing_validator_reports_sequential_fallback(self):
        result = workflow_decision_for_missing_validator()
        self.assertEqual(result, {"parallel_validation": "blocked", "execution": "sequential"})
```

- [ ] **Step 5: Run the CLI tests and verify RED**

Run: `python3 -m unittest tests.test_workflow_cli -v`

Expected: CLI entrypoint is missing.

- [ ] **Step 6: Implement the thin CLI and update skill references**

```python
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)
```

Document that covered dispatch requires a current receipt; unavailable or
incompatible CLI forces sequential fallback. Map changed-surface tags to exact
canonical reviewer agent names in the reviewer trigger matrix.

- [ ] **Step 7: Make release validation require the implementation**

Add required files and commands to `scripts/validate_repo.sh`:

```bash
require_file scripts/workflow
require_file scripts/validate_policy_contracts.py
require_file scripts/workflow_coordination/canonical_json.py
require_file tests/test_canonical_json.py
run python3 -m unittest tests.test_policy_contracts -v
run python3 -m unittest discover -s tests -p 'test_*coordination*.py' -v
run python3 -m unittest tests.test_workflow_cli -v
```

- [ ] **Step 8: Run focused and full verification**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
./scripts/validate_repo.sh
```

Expected: all tests and repository validation pass with no skipped required component.

- [ ] **Step 9: Commit**

```bash
git add scripts/workflow scripts/workflow_coordination tests skills README.md scripts/validate_repo.sh
git commit -m "feat: add coordination workflow CLI"
```

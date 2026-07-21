# Shared Agent Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make reviewer roles consistently read-only across Claude and Codex, provide canonical reviewer routing, and safely prepare—but not silently perform—the Claude global adapter installation.

**Architecture:** Common role files remain the source of truth under `$HOME/.agents/common-agents/`. A generic, conflict-safe installer in the public repository validates arbitrary source and target roots using temporary-directory tests; actual `$HOME/.claude/agents` installation stops after dry-run until the user explicitly approves the exact manifest.

**Tech Stack:** Python 3.9 standard library, `unittest`, filesystem symlinks, TOML parsing through an available Python 3.11+ interpreter only for optional adapter verification, existing skill/plugin validators.

## Global Constraints

- Reviewer roles are always read-only and hand fixes to Developer.
- Do not overwrite regular files, directories, or conflicting symlinks.
- Public repository validation uses temporary roots only.
- Actual global symlink creation requires a separate approval after dry-run.
- Project-local agents remain authoritative over global adapters.
- Source design: `docs/superpowers/specs/2026-07-21-risk-based-agent-workflow-validation-design.md`.

---

### Task 1: Generic Conflict-Safe Adapter Installer

**Files:**
- Create: `scripts/install_agent_adapters.py`
- Create: `tests/test_install_agent_adapters.py`

**Interfaces:**
- Consumes: `--source-root`, `--target-root`, `--suffix`, and `--dry-run`.
- Produces: JSON manifest with `create`, `keep`, `conflict`, and `error` entries; creates links only after explicit non-dry-run invocation.

- [ ] **Step 1: Write failing installer tests**

```python
class InstallerTests(unittest.TestCase):
    def test_dry_run_lists_links_without_mutation(self):
        result = plan_links(SOURCE, TARGET, suffix=".md")
        self.assertEqual(result.entries[0].action, "create")
        self.assertFalse((TARGET / "architect.md").exists())

    def test_correct_link_is_idempotent(self):
        (TARGET / "architect.md").symlink_to(SOURCE / "architect.md")
        result = plan_links(SOURCE, TARGET, suffix=".md")
        self.assertEqual(result.entries[0].action, "keep")

    def test_conflicting_file_is_never_overwritten(self):
        (TARGET / "architect.md").write_text("owned", encoding="utf-8")
        result = plan_links(SOURCE, TARGET, suffix=".md")
        self.assertEqual(result.entries[0].action, "conflict")
        with self.assertRaises(InstallConflict):
            apply_links(result)
        self.assertEqual((TARGET / "architect.md").read_text(), "owned")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_install_agent_adapters -v`

Expected: installer module import failure.

- [ ] **Step 3: Implement lstat-based planning and direct symlink creation**

```python
@dataclass(frozen=True)
class LinkEntry:
    name: str
    source: str
    target: str
    action: str
    reason: str


def plan_links(source_root: Path, target_root: Path, suffix: str) -> LinkPlan:
    """Resolve approved roots and classify every source entry without mutation."""


def apply_links(plan: LinkPlan) -> InstallResult:
    """Create direct symlinks; stop on EEXIST and never overwrite."""
```

Recompute and compare the plan hash immediately before applying. Reject source or
target paths outside approved roots and report partial application explicitly.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m unittest tests.test_install_agent_adapters -v`

Expected: dry-run, idempotency, conflict, root escape, and partial-failure tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/install_agent_adapters.py tests/test_install_agent_adapters.py
git commit -m "feat: add safe agent adapter installer"
```

### Task 2: Read-Only Reviewer Contract and Handoff

**Files:**
- Modify: `$HOME/.agents/common-agents/accessibility-reviewer.md`
- Modify: `$HOME/.agents/common-agents/api-reviewer.md`
- Modify: `$HOME/.agents/common-agents/code-reviewer.md`
- Modify: `$HOME/.agents/common-agents/performance-reviewer.md`
- Modify: `$HOME/.agents/common-agents/security-reviewer.md`
- Modify: `$HOME/.agents/common-agents/test-coverage-reviewer.md`
- Modify: `$HOME/.agents/common-agents/ux-reviewer.md`
- Modify: `$HOME/.agents/common-agents/qa-engineer.md`
- Modify: `$HOME/.agents/common-agents/pm.md`
- Create: `tests/test_shared_role_contracts.py`

**Interfaces:**
- Consumes: current common role contracts and the approved design.
- Produces: unconditionally read-only reviewers with `handoff_target`; QA owns planning/execution while Test Coverage independently audits.

- [ ] **Step 1: Write a failing local role-contract audit**

Create a temporary audit command in `tests/test_shared_role_contracts.py` that accepts `SHARED_AGENTS_ROOT`:

```python
class SharedRoleContractTests(unittest.TestCase):
    def test_reviewers_are_unconditionally_read_only(self):
        for role in REVIEWER_ROLES:
            text = (COMMON / f"{role}.md").read_text()
            self.assertIn("Read-only. Do not edit files.", text)
            self.assertNotIn("unless TOM explicitly asks for a fix", text)
            self.assertIn("handoff_target", text)

    def test_qa_and_coverage_have_distinct_ownership(self):
        self.assertIn("test planning and execution", QA_TEXT)
        self.assertIn("independent read-only audit", COVERAGE_TEXT)
```

- [ ] **Step 2: Run the audit and verify RED**

Run:

```bash
SHARED_AGENTS_ROOT="$HOME/.agents" python3 -m unittest tests.test_shared_role_contracts -v
```

Expected: reviewers that still permit direct fixes fail.

- [ ] **Step 3: Update common roles minimally**

Each reviewer Return section must include:

```markdown
- `handoff_target`: `developer`, `qa-engineer`, or another explicit implementation role
- `handoff_reason`: accepted finding and required change
```

Each reviewer Boundary must include exactly:

```markdown
- Read-only. Do not edit files.
- When TOM asks for a fix, return a concrete handoff to the applicable implementation role.
```

Define `qa-engineer` as test planning/execution/evidence owner and
`test-coverage-reviewer` as independent read-only assertion audit.

- [ ] **Step 4: Run the audit and verify GREEN**

Run:

```bash
SHARED_AGENTS_ROOT="$HOME/.agents" python3 -m unittest tests.test_shared_role_contracts -v
```

Expected: role boundaries and QA/coverage separation pass.

- [ ] **Step 5: Record the out-of-repository change**

Run: `git status --short`

Expected: only the repository test is shown; shared role changes are reported
separately because `$HOME/.agents` is not part of this Git repository.

### Task 3: Canonical Reviewer Routing and Adapter Audit

**Files:**
- Modify: `skills/adversarial-review-loop/references/reviewer-trigger-matrix.md`
- Modify: `skills/adversarial-review-loop/references/review-packet.md`
- Modify: `skills/adversarial-review-loop/SKILL.md`
- Create: `tests/test_reviewer_routing.py`
- Modify: `tests/test_shared_role_contracts.py`

**Interfaces:**
- Consumes: changed-surface tags and shared canonical role names.
- Produces: derived `reviewer_registry` entries and audits for Claude/Codex adapter alignment.

- [ ] **Step 1: Write failing canonical routing tests**

```python
class ReviewerRoutingTests(unittest.TestCase):
    def test_auth_surface_requires_security_and_qa_agents(self):
        derived = derive_reviewers({"auth"})
        self.assertEqual(derived, ("qa-engineer", "security-reviewer"))

    def test_required_reviewer_cannot_be_self_declared_complete(self):
        entry = reviewer_entry("security", "security-reviewer", required=True)
        self.assertEqual(entry.status, "pending")
        self.assertIsNone(entry.completion_evidence)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_reviewer_routing tests.test_shared_role_contracts -v`

Expected: canonical mapping parser/derivation is missing.

- [ ] **Step 3: Add exact lens-to-agent mappings and registry fields**

The matrix must use canonical names such as `security-reviewer`, `qa-engineer`,
`test-coverage-reviewer`, `accessibility-reviewer`, `performance-reviewer`,
`api-reviewer`, `architect`, and `code-reviewer`. The review packet records
required status, dispatch evidence, completion evidence, contract core hash, and
defer receipt.

- [ ] **Step 4: Verify adapter sets and parser health**

The audit must assert:

```python
self.assertEqual(common_names, claude_adapter_names)
self.assertEqual(common_names, codex_adapter_names)
self.assertEqual(broken_codex_links, [])
```

Use Python 3.12 `tomllib` when available for Codex TOML parsing; if unavailable,
record `not_run` rather than adding a dependency.

- [ ] **Step 5: Run tests and commit repository changes**

Run:

```bash
python3 -m unittest tests.test_reviewer_routing tests.test_shared_role_contracts -v
./scripts/validate_repo.sh
```

Then commit:

```bash
git add skills/adversarial-review-loop tests/test_reviewer_routing.py tests/test_shared_role_contracts.py
git commit -m "feat: align reviewer routing contracts"
```

### Task 4: Temporary Installer Validation and Real-Target Dry Run

**Files:**
- Modify: `scripts/validate_repo.sh`
- Modify: `README.md`
- Runtime artifact only: untracked dry-run JSON manifest in a mode-0700 temporary directory.

**Interfaces:**
- Consumes: installer from Task 1 and `$HOME/.agents/adapters/claude` as source.
- Produces: temporary-root integration evidence and a real-target dry-run manifest; does not create global links.

- [ ] **Step 1: Require installer tests in release validation**

Add:

```bash
require_file scripts/install_agent_adapters.py
require_file tests/test_install_agent_adapters.py
run python3 -m unittest tests.test_install_agent_adapters -v
```

- [ ] **Step 2: Run a temporary-root integration test**

Run:

```bash
tmp_root="$(mktemp -d)"
python3 scripts/install_agent_adapters.py \
  --source-root "$HOME/.agents/adapters/claude" \
  --target-root "$tmp_root/agents" \
  --suffix .md
test "$(find "$tmp_root/agents" -type l | wc -l | tr -d ' ')" = "16"
```

Expected: 16 links created under the temporary target only.

- [ ] **Step 3: Run the real-target dry run**

Run:

```bash
python3 scripts/install_agent_adapters.py \
  --source-root "$HOME/.agents/adapters/claude" \
  --target-root "$HOME/.claude/agents" \
  --suffix .md \
  --dry-run \
  --json
```

Expected: exact `create`/`keep`/`conflict` manifest with no filesystem mutation.

- [ ] **Step 4: Stop for explicit approval**

Present the manifest hash, target root, create count, keep count, and every
conflict. Do not run the non-dry-run command in the same approval step.

- [ ] **Step 5: Verify repository validation and commit**

Run: `./scripts/validate_repo.sh`

Expected: installer tests and all repository checks pass.

```bash
git add scripts/validate_repo.sh README.md
git commit -m "test: require shared adapter installer validation"
```

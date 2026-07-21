# Isolated Live Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an opt-in Codex live-evaluation runner with exact-checkout loading, fail-closed isolation, deterministic scenario assertions, bounded execution, and redacted evidence artifacts.

**Architecture:** A standard-library Python package builds and preflights an isolated Codex invocation before any model call. Scenario selection, budgets, command construction, redaction, artifact retention, and assertion reports are independently testable; the default repository validator tests the runner without making network or model calls.

**Tech Stack:** Python 3.9 standard library, `unittest`, Codex CLI 0.142.4+ feature detection, JSON Schema output, temporary directories.

## Global Constraints

- Live eval is never part of ordinary application validation.
- Automated auth accepts an explicitly supplied process-local API key only.
- OAuth-only environments return blocked evidence and never copy credential files.
- Agent subprocesses must not inherit the API key or credential-like variables.
- Tool, rule, plugin, hook, and skill isolation must fail closed.
- Raw output must be redacted before retained disk writes.
- Source design: `docs/superpowers/specs/2026-07-21-risk-based-agent-workflow-validation-design.md`.

---

### Task 1: Versioned Scenario Corpus and Assertions

**Files:**
- Create: `scripts/live_eval/__init__.py`
- Create: `scripts/live_eval/scenarios.py`
- Create: `tests/live-eval-scenarios.json`
- Create: `tests/test_live_eval_scenarios.py`

**Interfaces:**
- Consumes: canonical JSON scenario corpus.
- Produces: `load_scenarios(path) -> dict[str, Scenario]`, `select_scenarios(tags, limit) -> tuple[Scenario, ...]`, `assert_response(scenario, response) -> AssertionReport`.

- [ ] **Step 1: Write failing corpus tests**

```python
class ScenarioTests(unittest.TestCase):
    def test_selects_three_tagged_scenarios_by_default(self):
        scenarios = load_scenarios(FIXTURE)
        selected = select_scenarios(scenarios, {"workflow-intake"}, limit=3)
        self.assertEqual(len(selected), 3)
        self.assertTrue(all("workflow-intake" in item.tags for item in selected))

    def test_required_and_forbidden_assertions_are_deterministic(self):
        scenario = scenario_by_id("WI-MISSING-REPO")
        report = assert_response(scenario, {"next_step": "Which repo?", "autonomy_level": "L0"})
        self.assertTrue(report.passed)
        failure = assert_response(scenario, {"next_step": "Implemented it"})
        self.assertFalse(failure.passed)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_live_eval_scenarios -v`

Expected: import failure for scenario APIs.

- [ ] **Step 3: Implement the scenario model and JSON corpus**

```python
@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    schema_version: int
    tags: tuple[str, ...]
    prompt: str
    required_paths: tuple[str, ...]
    forbidden_values: tuple[str, ...]
    expected_status: str
    timeout_seconds: int
```

Convert the 26 natural-language acceptance scenarios into stable JSON entries.
Keep the original Markdown as human-readable documentation generated or checked
against the JSON IDs.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m unittest tests.test_live_eval_scenarios -v`

Expected: selection and assertion tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/live_eval tests/live-eval-scenarios.json tests/test_live_eval_scenarios.py
git commit -m "test: structure workflow live eval scenarios"
```

### Task 2: Isolated Codex Command and Authentication Gate

**Files:**
- Create: `scripts/live_eval/isolation.py`
- Create: `tests/test_live_eval_isolation.py`

**Interfaces:**
- Consumes: Codex executable path, model allowlist, temporary home/cwd, process API-key environment name.
- Produces: `build_invocation(config) -> Invocation`, `preflight_isolation(invocation) -> IsolationReport`.

- [ ] **Step 1: Write failing command and environment tests**

```python
class IsolationTests(unittest.TestCase):
    def test_builds_fail_closed_codex_command(self):
        invocation = build_invocation(TEST_CONFIG)
        self.assertIn("--ephemeral", invocation.argv)
        self.assertIn("--ignore-user-config", invocation.argv)
        self.assertIn("--ignore-rules", invocation.argv)
        self.assertIn("read-only", invocation.argv)
        self.assertIn("never", invocation.argv)

    def test_agent_environment_excludes_api_key(self):
        invocation = build_invocation(TEST_CONFIG)
        self.assertIn("OPENAI_API_KEY", invocation.transport_env)
        self.assertNotIn("OPENAI_API_KEY", invocation.tool_env)

    def test_oauth_only_is_blocked(self):
        report = preflight_auth(api_key=None, oauth_files_present=True)
        self.assertEqual(report.classification, "blocked_auth")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_live_eval_isolation -v`

Expected: import failure for isolation APIs.

- [ ] **Step 3: Implement command construction and preflight**

```python
@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    transport_env: dict[str, str]
    tool_env: dict[str, str]
    codex_home: Path
    cwd: Path


def build_invocation(config: EvalConfig) -> Invocation:
    """Build codex -a never exec with ephemeral/read-only/ignore flags."""


def preflight_isolation(invocation: Invocation) -> IsolationReport:
    """Verify CLI features and key non-inheritance before model execution."""
```

Use a neutral temporary cwd. Configure the Codex shell environment to inherit a
minimal allowlist and exclude the API-key name and all credential-like variables.
If CLI behavior cannot prove this separation, return `blocked_isolation`.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m unittest tests.test_live_eval_isolation -v`

Expected: invocation, OAuth block, and tool-environment tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/live_eval/isolation.py tests/test_live_eval_isolation.py
git commit -m "feat: isolate Codex live eval invocation"
```

### Task 3: Exact Checkout Loading and Preflight Hashes

**Files:**
- Create: `scripts/live_eval/checkout.py`
- Create: `tests/test_live_eval_checkout.py`

**Interfaces:**
- Consumes: repository checkout and isolated `CODEX_HOME`.
- Produces: `install_checkout_skills(...) -> CheckoutManifest`, `verify_loaded_checkout(...) -> PreflightResult`.

- [ ] **Step 1: Write failing exact-checkout tests**

```python
class CheckoutTests(unittest.TestCase):
    def test_installs_only_expected_skills_and_hashes_them(self):
        manifest = install_checkout_skills(REPO, TEMP_HOME)
        self.assertEqual(set(manifest.skill_names), {"workflow", "workflow-intake", "adversarial-review-loop"})
        self.assertEqual(manifest.tree_hash, git_tree_hash(REPO))

    def test_unexpected_copy_blocks_preflight(self):
        install_unexpected_skill(TEMP_HOME)
        result = verify_loaded_checkout(REPO, TEMP_HOME)
        self.assertEqual(result.classification, "blocked_isolation")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_live_eval_checkout -v`

Expected: checkout module import failure.

- [ ] **Step 3: Implement local links and fail-closed hash verification**

```python
@dataclass(frozen=True)
class CheckoutManifest:
    tree_hash: str
    plugin_manifest_hash: str
    skill_hashes: dict[str, str]
    skill_names: tuple[str, ...]


def install_checkout_skills(repo: Path, codex_home: Path) -> CheckoutManifest:
    """Link only the three checkout skills into an isolated home."""
```

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m unittest tests.test_live_eval_checkout -v`

Expected: exact-copy and unexpected-copy tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/live_eval/checkout.py tests/test_live_eval_checkout.py
git commit -m "feat: verify live eval checkout identity"
```

### Task 4: Budget, Streaming Redaction, and Artifact Lifecycle

**Files:**
- Create: `scripts/live_eval/artifacts.py`
- Create: `scripts/live_eval/budget.py`
- Create: `tests/test_live_eval_artifacts.py`
- Create: `tests/test_live_eval_budget.py`

**Interfaces:**
- Consumes: JSONL chunks, secret names/values, run policy.
- Produces: redacted mode-0600 artifacts, `BudgetDecision`, and blocked classifications.

- [ ] **Step 1: Write failing artifact and budget tests**

```python
class ArtifactTests(unittest.TestCase):
    def test_redacts_before_write(self):
        writer = RedactingWriter(TEMP_DIR, {"OPENAI_API_KEY": "secret-value"})
        writer.write(b'{"message":"secret-value /home/example"}\n')
        content = writer.path.read_text()
        self.assertNotIn("secret-value", content)
        self.assertNotIn("/home/example", content)
        self.assertEqual(stat.S_IMODE(writer.path.stat().st_mode), 0o600)

    def test_redaction_failure_retains_nothing(self):
        writer = RedactingWriter(TEMP_DIR, {}, redactor=FailingRedactor())
        with self.assertRaises(RedactionError):
            writer.write(b"raw")
        self.assertFalse(writer.path.exists())

class BudgetTests(unittest.TestCase):
    def test_targeted_budget_blocks_sixth_call(self):
        budget = Budget.targeted()
        for _ in range(5):
            budget.consume_call()
        self.assertEqual(budget.next_decision(), "blocked_budget")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_live_eval_artifacts tests.test_live_eval_budget -v`

Expected: artifact and budget module import failures.

- [ ] **Step 3: Implement fail-closed artifact and budget classes**

```python
@dataclass
class Budget:
    max_calls: int
    max_seconds: int
    concurrency: int
    calls_used: int = 0


class RedactingWriter:
    def write(self, chunk: bytes) -> None:
        """Redact in memory, then atomically append mode-0600 retained bytes."""
```

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python3 -m unittest tests.test_live_eval_artifacts tests.test_live_eval_budget -v`

Expected: redaction and targeted/release budget tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/live_eval/artifacts.py scripts/live_eval/budget.py tests/test_live_eval_artifacts.py tests/test_live_eval_budget.py
git commit -m "feat: bound and redact live eval artifacts"
```

### Task 5: Runner CLI and Non-Network Release Validation

**Files:**
- Create: `scripts/run_live_eval.py`
- Create: `tests/test_live_eval_runner.py`
- Modify: `scripts/validate_repo.sh`
- Modify: `README.md`
- Modify: `docs/forward-test-report.md`

**Interfaces:**
- Consumes: Tasks 1-4 modules and explicit scenario IDs/tags/model.
- Produces: run manifest, assertion report, blocked classifications, and optional live execution.

- [ ] **Step 1: Write failing runner tests with a fake Codex process**

```python
class RunnerTests(unittest.TestCase):
    def test_dry_run_never_calls_model(self):
        result = run_eval(EvalRequest.dry_run(tags=("workflow-intake",)), FakeCodex())
        self.assertEqual(result.status, "preflight_only")
        self.assertEqual(result.model_calls, 0)

    def test_infrastructure_retry_does_not_hide_failure(self):
        fake = FakeCodex([InvocationFailure(), InvocationFailure()])
        result = run_eval(EvalRequest.targeted(("WI-MISSING-REPO",)), fake)
        self.assertEqual(result.verification_result, "blocked")
        self.assertEqual(result.attempts, 2)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_live_eval_runner -v`

Expected: runner API is missing.

- [ ] **Step 3: Implement runner orchestration and CLI**

```python
def run_eval(request: EvalRequest, codex: CodexProcess) -> EvalResult:
    """Preflight, select, invoke, redact, assert, and report without silent retries."""


def main(argv=None) -> int:
    """Expose --scenario, --tags, --release-suite, --model, and --dry-run."""
```

- [ ] **Step 4: Require non-network runner tests in repository validation**

Add:

```bash
require_file scripts/run_live_eval.py
require_file tests/live-eval-scenarios.json
run python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
```

- [ ] **Step 5: Run verification without a live model call**

Run:

```bash
python3 scripts/run_live_eval.py --tags workflow-intake --model gpt-5.6-sol --dry-run
python3 -m unittest discover -s tests -p 'test_live_eval_*.py' -v
./scripts/validate_repo.sh
```

Expected: preflight-only result, zero model calls, all tests and repository validation pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_live_eval.py scripts/live_eval tests README.md docs/forward-test-report.md scripts/validate_repo.sh
git commit -m "feat: add isolated workflow live eval runner"
```

import ast
import os
import unittest
from pathlib import Path
from unittest import mock

from tests import test_shared_role_contracts as shared_contracts


_FORBIDDEN_PHASE_A_IMPORT_ROOTS = {
    "aiohttp",
    "builtins",
    "http",
    "httpx",
    "importlib",
    "os.environ",
    "os.getenv",
    "requests",
    "scripts.live_eval.artifacts",
    "scripts.live_eval.budget",
    "scripts.live_eval.isolation",
    "scripts.live_eval.scenarios",
    "scripts.run_live_eval",
    "shutil",
    "socket",
    "urllib",
}
_FORBIDDEN_PHASE_A_CALL_NAMES = {
    "__import__",
    "build_invocation",
    "eval",
    "exec",
    "preflight_auth",
    "preflight_isolation",
    "run_eval",
    "run_harness_dry_run",
}
_FORBIDDEN_OS_PROCESS_CALLS = {
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execlpe",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.popen",
    "os.posix_spawn",
    "os.posix_spawnp",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.system",
}
_PHASE_A_INTERNAL_DEPENDENCY_ALLOWLIST = {
    "experiment_plan.py": frozenset(
        {"scripts.workflow_coordination.canonical_json"}
    ),
    "experiment_receipts.py": frozenset(
        {
            "scripts.live_eval.experiment_plan",
            "scripts.live_eval.experiment_telemetry",
            "scripts.workflow_coordination.canonical_json",
        }
    ),
    "experiment_telemetry.py": frozenset(
        {"scripts.workflow_coordination.canonical_json"}
    ),
    "task_snapshot.py": frozenset(
        {
            "scripts.live_eval.experiment_receipts",
            "scripts.workflow_coordination.canonical_json",
        }
    ),
    "experiment.py": frozenset(
        {
            "scripts.live_eval.checkout",
            "scripts.live_eval.experiment_plan",
            "scripts.live_eval.experiment_receipts",
            "scripts.live_eval.harness",
            "scripts.live_eval.task_snapshot",
            "scripts.workflow_coordination.canonical_json",
        }
    ),
    "run_harness_experiment.py": frozenset(
        {
            "scripts.live_eval.experiment",
            "scripts.live_eval.experiment_plan",
            "scripts.live_eval.experiment_receipts",
            "scripts.live_eval.harness",
            "scripts.live_eval.task_snapshot",
        }
    ),
}


def _qualified_name(node, aliases):
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value, aliases)
        if parent is not None:
            return parent + "." + node.attr
    return None


def _phase_a_internal_dependencies(source, filename):
    parsed = ast.parse(source, filename=filename)
    dependencies = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            dependencies.update(
                alias.name
                for alias in node.names
                if alias.name == "scripts"
                or alias.name.startswith("scripts.")
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (
                node.module == "scripts"
                or node.module.startswith("scripts.")
            )
        ):
            dependencies.add(node.module)
    return frozenset(dependencies)


def _phase_a_internal_dependency_violations(source, filename):
    module_name = Path(filename).name
    allowed = _PHASE_A_INTERNAL_DEPENDENCY_ALLOWLIST[module_name]
    actual = _phase_a_internal_dependencies(source, filename)
    return tuple(
        "unexpected_internal_import:" + dependency
        for dependency in sorted(actual - allowed)
    ) + tuple(
        "missing_internal_import:" + dependency
        for dependency in sorted(allowed - actual)
    )


def _phase_a_dependency_violations(source, filename):
    parsed = ast.parse(source, filename=filename)
    module_name = Path(filename).name
    parents = {
        child: parent
        for parent in ast.walk(parsed)
        for child in ast.iter_child_nodes(parent)
    }
    aliases = {}
    imported = set()
    violations = []
    subprocess_imported = False
    exact_subprocess_imports = 0
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
                local = alias.asname or alias.name.split(".")[0]
                aliases[local] = (
                    alias.name
                    if alias.asname is not None
                    else alias.name.split(".")[0]
                )
                subprocess_imported |= alias.name == "subprocess"
                if alias.name == "subprocess" and alias.asname is None:
                    exact_subprocess_imports += 1
                elif alias.name == "subprocess":
                    violations.append("subprocess_import_shape")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level != 0:
                violations.append("relative_import")
            imported.add(module)
            subprocess_imported |= module == "subprocess"
            if module == "subprocess":
                violations.append("subprocess_import_shape")
            for alias in node.names:
                if alias.name == "*":
                    violations.append("wildcard_import")
                    continue
                local = alias.asname or alias.name
                aliases[local] = (
                    module + "." + alias.name
                    if module
                    else alias.name
                )

    imported.update(aliases.values())
    for name in imported:
        if any(
            name == root or name.startswith(root + ".")
            for root in _FORBIDDEN_PHASE_A_IMPORT_ROOTS
        ):
            violations.append("forbidden_import:" + name)

    task_snapshot = module_name == "task_snapshot.py"
    if subprocess_imported != task_snapshot:
        violations.append("subprocess_import_boundary")
    if task_snapshot and exact_subprocess_imports != 1:
        violations.append("subprocess_import_shape")
    popen_calls = 0
    sensitive_getattr_names = {
        "environ",
        "getenv",
        "Popen",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "system",
    }.union(
        name.rsplit(".", 1)[-1]
        for name in _FORBIDDEN_OS_PROCESS_CALLS
    )
    allowed_harness_calls = {
        "scripts.live_eval.harness.load_harness_source",
        "scripts.live_eval.harness.materialize_harness_home",
        "scripts.live_eval.harness.verify_loaded_harness",
    }
    forbidden_builtin_references = {
        "__builtins__",
        "__import__",
        "eval",
        "exec",
    }
    for node in ast.walk(parsed):
        if isinstance(node, (ast.Attribute, ast.Name)) and isinstance(
            node.ctx, ast.Load
        ):
            name = _qualified_name(node, aliases)
            if (
                name is not None
                and name.rsplit(".", 1)[-1]
                in forbidden_builtin_references
            ):
                violations.append(
                    "forbidden_builtin_reference:"
                    + name.rsplit(".", 1)[-1]
                )
        if isinstance(node, ast.Attribute):
            name = _qualified_name(node, aliases)
            if name in ("os.environ", "os.getenv"):
                violations.append("environment_access:" + name)
            if name in _FORBIDDEN_OS_PROCESS_CALLS:
                violations.append("process_reference:" + name)
        if isinstance(node, (ast.AnnAssign, ast.Assign, ast.NamedExpr)):
            value = node.value
            name = _qualified_name(value, aliases)
            if (
                name == "subprocess.Popen"
                or name in _FORBIDDEN_OS_PROCESS_CALLS
            ):
                violations.append("process_callable_escape:" + name)
        if not isinstance(node, ast.Call):
            continue
        name = _qualified_name(node.func, aliases)
        if name is not None:
            if name.rsplit(".", 1)[-1] in _FORBIDDEN_PHASE_A_CALL_NAMES:
                violations.append("forbidden_call:" + name)
            if name == "importlib.import_module":
                violations.append("dynamic_import")
            if name in _FORBIDDEN_OS_PROCESS_CALLS:
                violations.append("process_call:" + name)
            if name.startswith("os.environ.") or name == "os.getenv":
                violations.append("environment_call:" + name)
            if name.startswith("subprocess."):
                if task_snapshot and name == "subprocess.Popen":
                    popen_calls += 1
                    parent = parents.get(node)
                    while parent is not None and not isinstance(
                        parent, (ast.AsyncFunctionDef, ast.FunctionDef)
                    ):
                        parent = parents.get(parent)
                    if (
                        parent is None
                        or parent.name != "_run_git"
                    ):
                        violations.append("task_snapshot_popen_scope")
                else:
                    violations.append("subprocess_call:" + name)
        if (
            name in ("getattr", "builtins.getattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in sensitive_getattr_names
        ):
            violations.append("sensitive_getattr")
        if (
            module_name == "experiment.py"
            and name is not None
            and name.startswith("scripts.live_eval.checkout.")
        ):
            violations.append("checkout_callable:" + name)
        if (
            name is not None
            and name.startswith("scripts.live_eval.checkout.")
        ):
            violations.append("checkout_callable:" + name)
        if (
            name is not None
            and name.startswith("scripts.live_eval.harness.")
            and (
                module_name != "experiment.py"
                or name not in allowed_harness_calls
            )
        ):
            violations.append("harness_callable:" + name)
    expected_harness_names = {
        "experiment.py": {
            "HarnessError",
            "HarnessManifest",
            "HarnessPreflightResult",
            "HarnessSourceManifest",
            "load_harness_source",
            "materialize_harness_home",
            "verify_loaded_harness",
        },
        "run_harness_experiment.py": {"HarnessError"},
    }
    seen_harness_names = set()
    seen_checkout_names = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (
                    alias.name == "scripts.live_eval.checkout"
                    or alias.name.startswith(
                        "scripts.live_eval.checkout."
                    )
                ):
                    violations.append("checkout_import_shape")
                if (
                    alias.name == "scripts.live_eval.harness"
                    or alias.name.startswith(
                        "scripts.live_eval.harness."
                    )
                ):
                    violations.append("harness_import_shape")
        elif isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            if node.module == "scripts.live_eval.checkout":
                seen_checkout_names.update(names)
            elif (
                node.module is not None
                and node.module.startswith(
                    "scripts.live_eval.checkout."
                )
            ):
                violations.append("checkout_import_shape")
            if node.module == "scripts.live_eval.harness":
                seen_harness_names.update(names)
            elif (
                node.module is not None
                and node.module.startswith(
                    "scripts.live_eval.harness."
                )
            ):
                violations.append("harness_import_shape")
            if node.module == "scripts.live_eval":
                if "checkout" in names:
                    violations.append("checkout_import_shape")
                if "harness" in names:
                    violations.append("harness_import_shape")
    expected_checkout_names = (
        {"CheckoutManifest"}
        if module_name == "experiment.py"
        else set()
    )
    if seen_checkout_names != expected_checkout_names:
        violations.append("checkout_import_shape")
    if seen_harness_names != expected_harness_names.get(
        module_name, set()
    ):
        violations.append("harness_import_shape")
    if task_snapshot and popen_calls != 1:
        violations.append("task_snapshot_popen_count")
    if task_snapshot:
        subprocess_references = []

        def enclosing_scope(target):
            parent = parents.get(target)
            while parent is not None and not isinstance(
                parent, (ast.AsyncFunctionDef, ast.FunctionDef)
            ):
                parent = parents.get(parent)
            return parent.name if parent is not None else "<module>"

        for node in ast.walk(parsed):
            if isinstance(node, ast.Name) and isinstance(
                node.ctx, ast.Load
            ):
                name = _qualified_name(node, aliases)
                parent = parents.get(node)
                if name == "subprocess" and not (
                    isinstance(parent, ast.Attribute)
                    and parent.value is node
                ):
                    subprocess_references.append(
                        "module_escape:" + enclosing_scope(node)
                    )
                if (
                    name is None
                    or not name.startswith("subprocess.")
                ):
                    continue
            elif isinstance(node, ast.Attribute) and isinstance(
                node.ctx, ast.Load
            ):
                name = _qualified_name(node, aliases)
                if (
                    name is None
                    or not name.startswith("subprocess.")
                ):
                    continue
            else:
                continue

            member = name.rsplit(".", 1)[-1]
            parent = parents.get(node)
            scope = enclosing_scope(node)
            if (
                member == "Popen"
                and isinstance(parent, ast.arg)
                and parent.annotation is node
            ):
                record = "Popen:annotation:" + scope
            elif (
                member == "Popen"
                and isinstance(parent, ast.Call)
                and parent.func is node
            ):
                record = "Popen:call:" + scope
            elif (
                member == "Popen"
                and isinstance(parent, ast.Call)
                and node in parent.args
                and _qualified_name(parent.func, aliases)
                == "inspect.signature"
            ):
                record = "Popen:signature:" + scope
            elif (
                member == "TimeoutExpired"
                and isinstance(parent, ast.ExceptHandler)
                and parent.type is node
            ):
                record = "TimeoutExpired:except:" + scope
            elif (
                member in ("DEVNULL", "PIPE")
                and isinstance(parent, ast.keyword)
                and parent.value is node
            ):
                record = "{}:keyword:{}:{}".format(
                    member,
                    parent.arg,
                    scope,
                )
            else:
                record = "{}:unexpected:{}:{}".format(
                    member,
                    type(parent).__name__,
                    scope,
                )
            subprocess_references.append(record)

        expected_subprocess_references = (
            "DEVNULL:keyword:stdin:_run_git",
            "PIPE:keyword:stderr:_run_git",
            "PIPE:keyword:stdout:_run_git",
            "Popen:annotation:_cleanup_git_failure",
            "Popen:annotation:_cleanup_process",
            "Popen:annotation:_close_process_pipes",
            "Popen:call:_run_git",
            "Popen:signature:_require_supported_platform",
            "TimeoutExpired:except:_cleanup_process",
            "TimeoutExpired:except:_cleanup_process",
        )
        if tuple(sorted(subprocess_references)) != tuple(
            sorted(expected_subprocess_references)
        ):
            violations.append("task_snapshot_subprocess_surface")
    return tuple(sorted(set(violations)))


class RepositoryValidationTests(unittest.TestCase):
    _PHASE_A_REQUIRED_FILES = (
        ".github/workflows/validate.yml",
        "scripts/run_harness_experiment.py",
        "scripts/live_eval/experiment.py",
        "scripts/live_eval/experiment_plan.py",
        "scripts/live_eval/experiment_receipts.py",
        "scripts/live_eval/experiment_telemetry.py",
        "scripts/live_eval/task_snapshot.py",
        "tests/test_live_eval_experiment.py",
        "tests/test_live_eval_experiment_plan.py",
        "tests/test_live_eval_experiment_receipts.py",
        "tests/test_live_eval_experiment_telemetry.py",
        "tests/test_live_eval_task_snapshot.py",
        "tests/fixtures/harness_experiment/valid-plan-input.json",
        "tests/fixtures/harness_experiment/valid-terminal.jsonl",
    )

    def test_validator_runs_full_repository_owned_discovery(self):
        root = Path(__file__).parents[1]
        validator = (root / "scripts" / "validate_repo.sh").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            validator.count(
                "run python3 -m unittest discover -s tests -v"
            ),
            1,
        )
        self.assertNotIn(
            "python3 -m unittest tests.test_live_eval_",
            validator,
        )
        self.assertNotIn(
            "discover -s tests -p 'test_live_eval_",
            validator,
        )

    def test_validator_requires_each_phase_a_file_exactly_once(self):
        root = Path(__file__).parents[1]
        validator = (root / "scripts" / "validate_repo.sh").read_text(
            encoding="utf-8"
        )

        for relative in self._PHASE_A_REQUIRED_FILES:
            with self.subTest(relative=relative):
                self.assertEqual(
                    validator.count("require_file " + relative),
                    1,
                )

    def test_ci_pins_and_checks_the_python_39_baseline_once(self):
        root = Path(__file__).parents[1]
        workflow = (root / ".github" / "workflows" / "validate.yml")
        source = workflow.read_text(encoding="utf-8")
        checkout = source.index("uses: actions/checkout@v4")
        setup = source.index("uses: actions/setup-python@v7")
        baseline = source.index('python-version: "3.9"')
        assertion = source.index(
            "assert sys.version_info[:2] == (3, 9)"
        )
        validation = source.index("run: ./scripts/validate_repo.sh")

        self.assertEqual(source.count("uses: actions/setup-python@v7"), 1)
        self.assertEqual(source.count('python-version: "3.9"'), 1)
        self.assertEqual(
            source.count("assert sys.version_info[:2] == (3, 9)"),
            1,
        )
        self.assertLess(checkout, setup)
        self.assertLess(setup, baseline)
        self.assertLess(baseline, assertion)
        self.assertLess(assertion, validation)

    def test_plan_documents_exact_phase_a_internal_dependencies(self):
        root = Path(__file__).parents[1]
        plan_source = (
            root
            / "docs"
            / "superpowers"
            / "plans"
            / "2026-07-23-harness-experiment-readiness-plan.md"
        ).read_text(encoding="utf-8")
        dependency_cells = {
            Path(columns[1].strip("`")).name: columns[3]
            for line in plan_source.splitlines()
            for columns in ([cell.strip() for cell in line.split("|")],)
            if len(columns) >= 5
            and columns[1].startswith("`scripts/")
            and Path(columns[1].strip("`")).name
            in _PHASE_A_INTERNAL_DEPENDENCY_ALLOWLIST
        }

        self.assertEqual(
            set(dependency_cells),
            set(_PHASE_A_INTERNAL_DEPENDENCY_ALLOWLIST),
        )
        for module_name, allowed in (
            _PHASE_A_INTERNAL_DEPENDENCY_ALLOWLIST.items()
        ):
            documented = set(
                dependency_cells[module_name].split("`")[1::2]
            )
            expected = {
                dependency.rsplit(".", 1)[-1]
                for dependency in allowed
            }
            with self.subTest(module_name=module_name):
                self.assertEqual(documented, expected)

    def test_phase_a_internal_imports_follow_dependency_direction(self):
        root = Path(__file__).parents[1]
        targets = (
            root / "scripts" / "run_harness_experiment.py",
            root / "scripts" / "live_eval" / "experiment.py",
            root / "scripts" / "live_eval" / "experiment_plan.py",
            root / "scripts" / "live_eval" / "experiment_receipts.py",
            root / "scripts" / "live_eval" / "experiment_telemetry.py",
            root / "scripts" / "live_eval" / "task_snapshot.py",
        )

        for path in targets:
            with self.subTest(path=path):
                self.assertEqual(
                    _phase_a_internal_dependency_violations(
                        path.read_text(encoding="utf-8"),
                        str(path),
                    ),
                    (),
                )

    def test_phase_a_internal_dependency_gate_detects_direction_mutations(self):
        root = Path(__file__).parents[1]
        mutations = (
            (
                "scripts/live_eval/experiment_plan.py",
                "from scripts.live_eval.experiment_receipts "
                "import validate_runtime_records\n",
                "unexpected_internal_import:"
                "scripts.live_eval.experiment_receipts",
            ),
            (
                "scripts/run_harness_experiment.py",
                "from scripts.live_eval.experiment_telemetry "
                "import parse_telemetry_jsonl\n",
                "unexpected_internal_import:"
                "scripts.live_eval.experiment_telemetry",
            ),
            (
                "scripts/live_eval/experiment.py",
                "import scripts.live_eval.experiment_telemetry "
                "as telemetry\n",
                "unexpected_internal_import:"
                "scripts.live_eval.experiment_telemetry",
            ),
        )

        for relative, mutation, expected in mutations:
            path = root / relative
            source = path.read_text(encoding="utf-8")
            with self.subTest(relative=relative):
                self.assertIn(
                    expected,
                    _phase_a_internal_dependency_violations(
                        source + "\n" + mutation,
                        relative,
                    ),
                )

        telemetry_path = (
            root / "scripts" / "live_eval" / "experiment_telemetry.py"
        )
        telemetry_source = telemetry_path.read_text(encoding="utf-8")
        canonical_import = (
            "from scripts.workflow_coordination.canonical_json "
            "import canonical_bytes\n"
        )
        self.assertEqual(telemetry_source.count(canonical_import), 1)
        without_canonical_import = telemetry_source.replace(
            canonical_import,
            "",
            1,
        )
        self.assertIn(
            "missing_internal_import:"
            "scripts.workflow_coordination.canonical_json",
            _phase_a_internal_dependency_violations(
                without_canonical_import,
                str(telemetry_path),
            ),
        )

    def test_phase_a_modules_have_no_live_or_network_dependency_seam(self):
        root = Path(__file__).parents[1]
        targets = (
            root / "scripts" / "run_harness_experiment.py",
            root / "scripts" / "live_eval" / "experiment.py",
            root / "scripts" / "live_eval" / "experiment_plan.py",
            root / "scripts" / "live_eval" / "experiment_receipts.py",
            root / "scripts" / "live_eval" / "experiment_telemetry.py",
            root / "scripts" / "live_eval" / "task_snapshot.py",
        )

        for path in targets:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertEqual(
                    _phase_a_dependency_violations(
                        source, str(path)
                    ),
                    (),
                )

    def test_phase_a_dependency_gate_detects_alias_and_dynamic_bypasses(self):
        root = Path(__file__).parents[1]
        sources = {
            relative: (root / relative).read_text(encoding="utf-8")
            for relative in (
                "scripts/live_eval/experiment.py",
                "scripts/live_eval/experiment_plan.py",
                "scripts/live_eval/task_snapshot.py",
            )
        }
        mutations = (
            (
                "scripts/live_eval/experiment.py",
                "import socket as transport\n"
                "transport.create_connection(())\n",
                "forbidden_import:socket",
            ),
            (
                "scripts/live_eval/experiment.py",
                "from scripts import run_live_eval as legacy\n"
                "legacy.run_eval(None, None)\n",
                "forbidden_import:scripts.run_live_eval",
            ),
            (
                "scripts/live_eval/experiment.py",
                "from os import system as launch\nlaunch('command')\n",
                "process_call:os.system",
            ),
            (
                "scripts/live_eval/experiment.py",
                "import importlib as loader\n"
                "loader.import_module('socket')\n",
                "dynamic_import",
            ),
            (
                "scripts/live_eval/experiment.py",
                "from os import environ as credentials\n"
                "credentials.get('SYNTHETIC_KEY')\n",
                "forbidden_import:os.environ",
            ),
            (
                "scripts/live_eval/experiment.py",
                "import subprocess as process\nprocess.Popen(())\n",
                "subprocess_import_boundary",
            ),
            (
                "scripts/live_eval/experiment.py",
                "getattr(os, 'system')('command')\n",
                "sensitive_getattr",
            ),
            (
                "scripts/live_eval/experiment.py",
                "from scripts.live_eval.isolation "
                "import preflight_auth as auth\n"
                "auth(None)\n",
                "forbidden_import:scripts.live_eval.isolation",
            ),
            (
                "scripts/live_eval/experiment.py",
                "from os import getenv as read_secret\n"
                "read_secret('SYNTHETIC_KEY')\n",
                "forbidden_import:os.getenv",
            ),
            (
                "scripts/live_eval/task_snapshot.py",
                "from subprocess import Popen as Launch\n"
                "Launch(('git',))\n",
                "subprocess_import_shape",
            ),
            (
                "scripts/live_eval/task_snapshot.py",
                "subprocess.Popen(('git',))\n",
                "task_snapshot_popen_scope",
            ),
            (
                "scripts/live_eval/experiment.py",
                "import scripts.live_eval.checkout as checkout\n"
                "checkout.install_checkout_skills(None, None)\n",
                "checkout_import_shape",
            ),
            (
                "scripts/live_eval/task_snapshot.py",
                "def _mutation_escape():\n"
                "    launch = subprocess.Popen\n"
                "    launch(('codex',))\n",
                "process_callable_escape:subprocess.Popen",
            ),
            (
                "scripts/live_eval/experiment.py",
                "launch = os.system\nlaunch('command')\n",
                "process_callable_escape:os.system",
            ),
            (
                "scripts/live_eval/task_snapshot.py",
                "getattr(subprocess, 'Popen')(('codex',))\n",
                "sensitive_getattr",
            ),
            (
                "scripts/live_eval/experiment.py",
                "import scripts.live_eval.harness as harness\n"
                "harness.install_checkout_skills(None, None)\n",
                "harness_import_shape",
            ),
            (
                "scripts/live_eval/experiment.py",
                "from scripts.live_eval import harness\n"
                "harness.install_checkout_skills(None, None)\n",
                "harness_import_shape",
            ),
            (
                "scripts/live_eval/experiment_plan.py",
                "from scripts.live_eval.checkout "
                "import install_checkout_skills\n"
                "install_checkout_skills(None, None)\n",
                "checkout_import_shape",
            ),
            (
                "scripts/live_eval/experiment.py",
                "from os import environ as credentials\n"
                "value = credentials['SYNTHETIC_KEY']\n",
                "forbidden_import:os.environ",
            ),
            (
                "scripts/live_eval/experiment.py",
                "loader = __import__\nloader('socket')\n",
                "forbidden_builtin_reference:__import__",
            ),
            (
                "scripts/live_eval/experiment.py",
                "(loader := __import__)('socket')\n",
                "forbidden_builtin_reference:__import__",
            ),
            (
                "scripts/live_eval/experiment_plan.py",
                "from . import budget as paid\npaid.Budget()\n",
                "relative_import",
            ),
            (
                "scripts/live_eval/experiment_plan.py",
                "from .checkout import install_checkout_skills\n",
                "relative_import",
            ),
            (
                "scripts/live_eval/task_snapshot.py",
                "(launch,) = (subprocess.Popen,)\n"
                "launch(('codex',))\n",
                "task_snapshot_subprocess_surface",
            ),
            (
                "scripts/live_eval/task_snapshot.py",
                "def _extra_launch(launch=subprocess.Popen):\n"
                "    launch(('codex',))\n",
                "task_snapshot_subprocess_surface",
            ),
            (
                "scripts/live_eval/task_snapshot.py",
                "import functools\n"
                "functools.partial(subprocess.Popen, ('codex',))\n",
                "task_snapshot_subprocess_surface",
            ),
            (
                "scripts/live_eval/task_snapshot.py",
                "launch = subprocess.run\nlaunch(('codex',))\n",
                "task_snapshot_subprocess_surface",
            ),
        )
        for filename, mutation, expected in mutations:
            with self.subTest(
                filename=filename,
                expected=expected,
            ):
                violations = _phase_a_dependency_violations(
                    sources[filename] + "\n" + mutation,
                    filename,
                )
                self.assertIn(expected, violations)

    def test_experiment_uses_only_public_harness_checkout_api(self):
        root = Path(__file__).parents[1]
        source = (
            root / "scripts" / "live_eval" / "experiment.py"
        ).read_text(encoding="utf-8")
        parsed = ast.parse(source)
        harness_names = set()
        checkout_names = set()
        for node in ast.walk(parsed):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module == "scripts.live_eval.harness":
                harness_names.update(alias.name for alias in node.names)
            elif node.module == "scripts.live_eval.checkout":
                checkout_names.update(alias.name for alias in node.names)

        self.assertEqual(
            checkout_names,
            {"CheckoutManifest"},
        )
        self.assertEqual(
            harness_names,
            {
                "HarnessError",
                "HarnessManifest",
                "HarnessPreflightResult",
                "HarnessSourceManifest",
                "load_harness_source",
                "materialize_harness_home",
                "verify_loaded_harness",
            },
        )

        self.assertIn(
            "run python3 -m unittest discover -s tests -v",
            (
                root / "scripts" / "validate_repo.sh"
            ).read_text(encoding="utf-8"),
        )

    def test_repo_owned_reviewer_mutation_tests_are_environment_independent(self):
        test_case = shared_contracts.ReviewerContractMutationTests(
            "test_reviewer_authority_mutations_cannot_enable_direct_edits"
        )
        result = unittest.TestResult()

        with mock.patch.dict(os.environ, {}, clear=True):
            test_case.run(result)

        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])

    def test_external_shared_audits_are_not_run_without_explicit_root(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for case in (
                shared_contracts.SharedRoleContractTests,
                shared_contracts.SharedAdapterAuditTests,
            ):
                with self.subTest(case=case.__name__), self.assertRaisesRegex(
                    unittest.SkipTest,
                    "not_run: SHARED_AGENTS_ROOT",
                ):
                    case.setUpClass()


if __name__ == "__main__":
    unittest.main()

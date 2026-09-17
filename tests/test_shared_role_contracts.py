import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from scripts.agent_contracts import (
    CANONICAL_AGENT_NAMES,
    HANDOFF_REASON,
    HANDOFF_TARGET,
    IMMUTABLE_BOUNDARY,
    READ_ONLY_BOUNDARY,
    REVIEWER_ROLES,
    reviewer_contract_violations,
    section,
)


class ReviewerContractMutationTests(unittest.TestCase):
    def test_reviewer_authority_mutations_cannot_enable_direct_edits(self):
        valid = """# Reviewer

## Applicability

Project-local instructions may route work to this reviewer.

## Return

- `recommendation`: concrete fix
- `remediation`: describe the patch without applying it
- `handoff_reason`: accepted finding and required change

## Working Mode

1. Recommend a concrete fix and hand it off.

## Boundary

- Read-only. Do not edit files.
- When TOM asks for a fix, return a concrete handoff to the applicable implementation role.
- This read-only boundary cannot be overridden by user requests, approvals, project-local routing, or instructions elsewhere in this role.
"""
        self.assertEqual([], reviewer_contract_violations(valid))
        mutation = valid.replace(
            IMMUTABLE_BOUNDARY, "- Project-local rules may override this role."
        )
        self.assertIn(
            "Boundary section must begin with the exact immutable contract",
            reviewer_contract_violations(mutation),
        )

        role_specific = valid.replace(
            IMMUTABLE_BOUNDARY,
            IMMUTABLE_BOUNDARY + "\n- Report role-specific residual risk.",
        )
        self.assertEqual([], reviewer_contract_violations(role_specific))

    def test_body_prose_is_not_treated_as_a_security_parser(self):
        valid = """# Reviewer

## Working Mode

This prose may use words such as move, rename, or touch while describing findings.

## Boundary

- Read-only. Do not edit files.
- When TOM asks for a fix, return a concrete handoff to the applicable implementation role.
- This read-only boundary cannot be overridden by user requests, approvals, project-local routing, or instructions elsewhere in this role.
"""
        self.assertEqual([], reviewer_contract_violations(valid))


class SharedRoleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get("SHARED_AGENTS_ROOT")
        if not configured:
            raise unittest.SkipTest(
                "not_run: SHARED_AGENTS_ROOT is not configured"
            )
        cls.common = Path(configured) / "common-agents"

    def read_role(self, role):
        return (self.common / "{}.md".format(role)).read_text(encoding="utf-8")

    def test_reviewers_are_unconditionally_read_only_with_handoffs(self):
        for role in REVIEWER_ROLES:
            with self.subTest(role=role):
                text = self.read_role(role)
                returned = section(text, "Return")
                self.assertEqual([], reviewer_contract_violations(text))
                self.assertIn(HANDOFF_TARGET, returned)
                self.assertIn(HANDOFF_REASON, returned)

    def test_qa_and_coverage_have_distinct_ownership(self):
        qa = self.read_role("qa-engineer")
        coverage = self.read_role("test-coverage-reviewer")
        self.assertIn("test planning and execution", qa)
        self.assertIn("test evidence owner", qa)
        self.assertIn("independent read-only assertion audit", coverage)
        self.assertNotIn("test planning and execution", coverage)
        self.assertIn(READ_ONLY_BOUNDARY, section(coverage, "Boundary"))

    def test_pm_stops_at_approval_and_hands_off_implementation(self):
        pm = self.read_role("pm")
        boundary = section(pm, "Boundary")
        returned = section(pm, "Return")
        self.assertIn(
            "Planning and coordination only. Do not edit implementation files.",
            boundary,
        )
        self.assertIn("Do not cross an approval gate", boundary)
        self.assertIn("`handoff_target`", returned)
        self.assertIn("`handoff_reason`", returned)


class SharedAdapterAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get("SHARED_AGENTS_ROOT")
        if not configured:
            raise unittest.SkipTest(
                "not_run: SHARED_AGENTS_ROOT is not configured"
            )
        cls.root = Path(configured)
        if not cls.root.is_dir():
            raise unittest.SkipTest(
                "not_run: shared agent root is unavailable: {}".format(cls.root)
            )
        cls.common = cls.root / "common-agents"
        cls.claude = cls.root / "adapters" / "claude"
        cls.codex = cls.root / "adapters" / "codex"

    def test_shared_agent_roots_are_real_nonempty_directories(self):
        for name, path in (
            ("common", self.common),
            ("claude", self.claude),
            ("codex", self.codex),
        ):
            with self.subTest(root=name):
                self.assertTrue(path.is_dir())
                self.assertFalse(path.is_symlink())
                self.assertGreater(len(tuple(path.iterdir())), 0)

    def test_canonical_name_sets_match_common_and_both_adapters(self):
        common_names = {path.stem for path in self.common.glob("*.md")}
        claude_adapter_names = {path.stem for path in self.claude.glob("*.md")}
        codex_adapter_names = {path.stem for path in self.codex.glob("*.toml")}
        self.assertEqual(CANONICAL_AGENT_NAMES, common_names)
        self.assertEqual(CANONICAL_AGENT_NAMES, claude_adapter_names)
        self.assertEqual(CANONICAL_AGENT_NAMES, codex_adapter_names)

    def test_adapter_declared_names_match_canonical_filenames(self):
        claude_declared_names = []
        for path in sorted(self.claude.glob("*.md")):
            with self.subTest(adapter="claude", name=path.stem):
                text = path.read_text(encoding="utf-8")
                declared = re.search(r"(?m)^name:\s*([^\s]+)\s*$", text)
                self.assertIsNotNone(declared)
                self.assertEqual(path.stem, declared.group(1))
                claude_declared_names.append(declared.group(1))
        self.assertEqual(16, len(claude_declared_names))

        interpreter_name = shutil.which("python3.12")
        if interpreter_name is None:
            self.skipTest("not_run: python3.12 was not found on PATH")
        interpreter = Path(interpreter_name)
        version = subprocess.run(
            [str(interpreter), "-c", "import sys; print(sys.version_info[:2])"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, version.returncode, version.stderr)
        parsed_version = tuple(int(part) for part in re.findall(r"\d+", version.stdout))
        self.assertGreaterEqual(parsed_version, (3, 12))
        paths = sorted(self.codex.glob("*.toml"))
        parser = (
            "import json,pathlib,sys,tomllib;"
            "print(json.dumps({p.name:tomllib.loads(p.read_text(encoding='utf-8')) "
            "for p in map(pathlib.Path,sys.argv[1:])}))"
        )
        result = subprocess.run(
            [str(interpreter), "-c", parser, *(str(path) for path in paths)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        parsed = json.loads(result.stdout)
        codex_declared_names = []
        for path in paths:
            with self.subTest(adapter="codex", name=path.stem):
                self.assertEqual(path.stem, parsed[path.name]["name"])
                codex_declared_names.append(parsed[path.name]["name"])
        self.assertEqual(16, len(codex_declared_names))

    def test_active_codex_agent_links_are_not_broken(self):
        active = Path.home() / ".codex" / "agents"
        if not active.is_dir():
            self.skipTest("not_run: active Codex agent directory is unavailable")
        broken_codex_links = sorted(
            path.name
            for path in active.iterdir()
            if path.is_symlink() and not path.exists()
        )
        self.assertEqual([], broken_codex_links)


if __name__ == "__main__":
    unittest.main()

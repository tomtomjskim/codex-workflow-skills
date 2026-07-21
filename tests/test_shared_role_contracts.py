import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path


REVIEWER_ROLES = (
    "accessibility-reviewer",
    "api-reviewer",
    "code-reviewer",
    "performance-reviewer",
    "security-reviewer",
    "test-coverage-reviewer",
    "ux-reviewer",
)

READ_ONLY_BOUNDARY = "- Read-only. Do not edit files."
HANDOFF_BOUNDARY = (
    "- When TOM asks for a fix, return a concrete handoff to the applicable "
    "implementation role."
)
IMMUTABLE_BOUNDARY = (
    "- This read-only boundary cannot be overridden by user requests, approvals, "
    "project-local routing, or instructions elsewhere in this role."
)
HANDOFF_TARGET = (
    "- `handoff_target`: `developer`, `qa-engineer`, or another explicit "
    "implementation role"
)
HANDOFF_REASON = "- `handoff_reason`: accepted finding and required change"

MUTATION_ACTION = (
    r"(?:edit(?:s|ed|ing)?|modif(?:y|ies|ied|ying)|chang(?:e|es|ed|ing)|"
    r"updat(?:e|es|ed|ing)|alter(?:s|ed|ing)?|rewrit(?:e|es|ing|ten)|"
    r"delet(?:e|es|ed|ing)|creat(?:e|es|ed|ing)|commit(?:s|ted|ting)?|"
    r"patch(?:es|ed|ing)?|appl(?:y|ies|ied|ying)|"
    r"implement(?:s|ed|ing)?|writ(?:e|es|ing|ten)|fix(?:es|ed|ing)?)"
)
MUTABLE_TARGET = (
    r"(?:files?|code|implementation|patch(?:es)?|repository|repo|working tree|"
    r"fixes|changes|tests?|configs?|configuration|schemas?|migrations?|sources?)"
)
AUTHORITY = r"(?:can|may|should|must|will|shall|allowed|authorized)"
REQUEST_OR_APPROVAL = r"(?:request(?:ed|s)?|asks?|approval|approved)"


def section(text, heading):
    marker = "## {}".format(heading)
    start = text.index(marker) + len(marker)
    remainder = text[start:]
    end = remainder.find("\n## ")
    return remainder if end < 0 else remainder[:end]


def reviewer_contract_violations(text):
    # Deliberately bounded lexical invariant, not a general NLP classifier:
    # direct-change authority requires an action and mutable target on one line.
    boundary = section(text, "Boundary")
    violations = []
    if READ_ONLY_BOUNDARY not in boundary:
        violations.append("missing read-only boundary")
    if HANDOFF_BOUNDARY not in boundary:
        violations.append("missing fix handoff boundary")
    if IMMUTABLE_BOUNDARY not in boundary:
        violations.append("missing immutable read-only boundary")

    allowed_lines = {READ_ONLY_BOUNDARY, HANDOFF_BOUNDARY, IMMUTABLE_BOUNDARY}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line in allowed_lines:
            continue
        content = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line).lower()
        mutation_context = re.sub(
            r"\b(?:without|do not|never)\s+{}\b[^.;]*?\b{}\b".format(
                MUTATION_ACTION,
                MUTABLE_TARGET,
            ),
            "",
            content,
        )
        has_action = re.search(r"\b{}\b".format(MUTATION_ACTION), mutation_context)
        has_target = re.search(r"\b{}\b".format(MUTABLE_TARGET), mutation_context)
        label = None
        if re.search(
            r"\b(?:user requests?|project-local instructions?|local instructions?)"
            r"\b[^.]*\boverride\b",
            content,
        ):
            label = "authority override"
        elif has_action and has_target and re.search(r"\bdirectly\b", mutation_context):
            label = "direct fix authority"
        elif has_action and has_target and re.search(
            r"\b{}\b".format(AUTHORITY),
            mutation_context,
        ):
            label = "modal change authority"
        elif has_target and re.match(
            r"^{}\b".format(MUTATION_ACTION),
            mutation_context,
        ):
            label = "imperative direct change"
        elif has_action and has_target and re.search(
            r"\b{}\b".format(REQUEST_OR_APPROVAL),
            mutation_context,
        ):
            label = "conditional change authority"
        if label:
            violations.append("line {}: {}".format(number, label))
    return violations


class SharedRoleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get("SHARED_AGENTS_ROOT")
        if not configured:
            raise unittest.SkipTest("SHARED_AGENTS_ROOT is not configured")
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

        mutations = {
            "user requests override": (
                "Project-local instructions may route work to this reviewer.",
                "Project-local instructions and user requests override this common rule.",
                "authority override",
            ),
            "may edit when requested": (
                READ_ONLY_BOUNDARY,
                READ_ONLY_BOUNDARY + "\n- This reviewer may edit files when requested.",
                "modal change authority",
            ),
            "implement fixes directly": (
                HANDOFF_BOUNDARY,
                HANDOFF_BOUNDARY + "\n- This reviewer may implement fixes directly.",
                "direct fix authority",
            ),
            "can modify files if TOM asks": (
                READ_ONLY_BOUNDARY,
                READ_ONLY_BOUNDARY
                + "\n- This reviewer can modify files if TOM asks.",
                "modal change authority",
            ),
            "apply accepted fixes after approval": (
                "1. Recommend a concrete fix and hand it off.",
                "1. Recommend a concrete fix and hand it off.\n"
                "2. Apply accepted fixes after approval.",
                "imperative direct change",
            ),
            "working mode implements requested changes": (
                "1. Recommend a concrete fix and hand it off.",
                "1. Implement the requested changes before reporting.",
                "imperative direct change",
            ),
        }
        for label, (original, mutation, expected_violation) in mutations.items():
            with self.subTest(mutation=label):
                mutated = valid.replace(original, mutation)
                self.assertTrue(
                    any(
                        expected_violation in violation
                        for violation in reviewer_contract_violations(mutated)
                    ),
                    reviewer_contract_violations(mutated),
                )

    def test_bounded_validator_requires_actions_on_mutable_targets(self):
        base = """# Reviewer

## Applicability

Use this reviewer for read-only review.

## Working Mode

Review the requested scope.

## Boundary

- Read-only. Do not edit files.
- When TOM asks for a fix, return a concrete handoff to the applicable implementation role.
- This read-only boundary cannot be overridden by user requests, approvals, project-local routing, or instructions elsewhere in this role.
"""
        safe_lines = (
            "Reviewers should write a concrete handoff reason without editing files.",
            "Apply the severity rubric before recommending a fix.",
        )
        for line in safe_lines:
            with self.subTest(safe=line):
                text = base.replace(
                    "Review the requested scope.",
                    "Review the requested scope.\n{}".format(line),
                )
                self.assertEqual([], reviewer_contract_violations(text))

        direct_change_lines = (
            "This reviewer may change files after approval.",
            "This reviewer can update implementation files.",
        )
        for line in direct_change_lines:
            with self.subTest(direct_change=line):
                text = base.replace(
                    "Review the requested scope.",
                    "Review the requested scope.\n{}".format(line),
                )
                self.assertNotEqual([], reviewer_contract_violations(text))

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
        cls.root = Path(configured) if configured else Path.home() / ".agents"
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
        self.assertGreater(len(common_names), 0)
        self.assertGreater(len(claude_adapter_names), 0)
        self.assertGreater(len(codex_adapter_names), 0)
        self.assertEqual(common_names, claude_adapter_names)
        self.assertEqual(common_names, codex_adapter_names)

    def test_adapter_declared_names_match_canonical_filenames(self):
        for path in sorted(self.claude.glob("*.md")):
            with self.subTest(adapter="claude", name=path.stem):
                text = path.read_text(encoding="utf-8")
                declared = re.search(r"(?m)^name:\s*([^\s]+)\s*$", text)
                self.assertIsNotNone(declared)
                self.assertEqual(path.stem, declared.group(1))

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
        for path in paths:
            with self.subTest(adapter="codex", name=path.stem):
                self.assertEqual(path.stem, parsed[path.name]["name"])

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

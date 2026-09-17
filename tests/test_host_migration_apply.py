import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import host_migration_apply, host_migration_envelope
from scripts.host_migration_snapshot import rollback, snapshot


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "host_migration_apply.py"
ENVELOPE_SCRIPT = ROOT / "scripts" / "host_migration_envelope.py"
TARGET_CONFIG = '[mcp_servers.db-mcp]\nenabled = false\n'
POLICY_SHA256 = host_migration_apply._canonical_policy_sha256(
    (ROOT / "policies" / "host-policy.json").read_bytes()
)
PYTHON = "/usr/bin/python3"
CODEX = PYTHON


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_ref(path):
    return {"path": str(path), "sha256": sha256(path)}


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *map(str, args)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class ApplyFixture:
    def __init__(self, root, reviewer_target=None):
        self.root = Path(root).resolve()
        self.host = self.root / "home"
        self.codex = self.host / ".codex"
        (self.codex / "rules").mkdir(parents=True)
        self.first = self.codex / "config.toml"
        self.second = self.codex / "rules" / "default.rules"
        self.scout_profile = self.codex / "scout.config.toml"
        self.profile = self.codex / "builder.config.toml"
        self.operator_profile = self.codex / "operator.config.toml"
        self.serena_config = self.host / ".serena" / "serena_config.yml"
        self.serena_projects = {
            name: self.host / "dev" / name / ".serena" / "project.yml"
            for name in ("project-one", "project-two")
        }
        self.serena_config.parent.mkdir(parents=True)
        for project_path in self.serena_projects.values():
            project_path.parent.mkdir(parents=True)
        self.first.write_text("current config\n", encoding="utf-8")
        self.second.write_text("current rules\n", encoding="utf-8")
        self.first.chmod(0o600)
        self.second.chmod(0o640)
        self.reviewer = None
        if reviewer_target is not None:
            self.reviewer = (
                self.host / ".agents" / "adapters" / "codex" / "dba.toml"
            )
            self.reviewer.parent.mkdir(parents=True)
            self.reviewer.write_text(
                '[mcp_servers.db-mcp]\nenabled = false\n', encoding="utf-8"
            )
            self.reviewer.chmod(0o600)

        self.target_root = self.root / "target"
        self.target_root.mkdir()
        self.artifacts = {}
        artifact_specs = [
            (self.first, TARGET_CONFIG, 0o600),
            (self.second, "target rules\n", 0o644),
            (
                self.scout_profile,
                'sandbox_mode = "read-only"\n[mcp_servers.db-mcp]\nenabled = false\n',
                0o600,
            ),
            (
                self.profile,
                'sandbox_mode = "workspace-write"\n[mcp_servers.db-mcp]\nenabled = false\n',
                0o600,
            ),
            (
                self.operator_profile,
                'sandbox_mode = "workspace-write"\n[mcp_servers.db-mcp]\nenabled = false\n',
                0o600,
            ),
            (self.serena_config, "web_dashboard: false\n", 0o600),
            *[
                (path, "read_only: true\nlanguages:\n  - python\n", 0o600)
                for path in self.serena_projects.values()
            ],
        ]
        if self.reviewer is not None:
            artifact_specs.append((self.reviewer, reviewer_target, 0o600))
        for source, content, mode in artifact_specs:
            artifact = self.target_root / source.relative_to(source.anchor)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(content, encoding="utf-8")
            artifact.chmod(mode)
            self.artifacts[source] = artifact

        self.records = [
            self._present("codex-config", self.first),
            self._present("codex-rules", self.second),
            self._absent("codex-profile-scout", self.scout_profile),
            self._absent("codex-profile-builder", self.profile),
            self._absent("codex-profile-operator", self.operator_profile),
            self._absent("serena-config", self.serena_config),
            *[
                self._absent("serena-project-{}".format(name), path)
                for name, path in self.serena_projects.items()
            ],
        ]
        if self.reviewer is not None:
            self.records.append(self._present("codex-adapter-dba", self.reviewer))
        audit_inputs = [
            {
                "label": host_migration_apply._expected_target_receipt_label(
                    record["label"]
                ).rstrip("*"),
                "path": str(self.target_root / Path(record["path"]).relative_to(Path(record["path"]).anchor)),
                "mode": record["target_mode"],
                "sha256": record["target_sha256"],
            }
            for record in self.records
        ]
        routing_path = (
            ROOT
            / "skills/adversarial-review-loop/references/reviewer-routing.json"
        )
        audit_inputs.append(
            {
                "label": "reviewer routing",
                "path": str(routing_path),
                "mode": "{:04o}".format(routing_path.stat().st_mode & 0o7777),
                "sha256": sha256(routing_path),
            }
        )
        canonical_inputs = json.dumps(
            audit_inputs, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.manifest = self.root / "manifest.json"
        manifest_payload = {
                    "schema_version": 3,
                    "audit": {
                        "status": "ok",
                        "scope": "target",
                        "stage": "full",
                        "validator": "validate_host_policy.py@3",
                        "policy_sha256": POLICY_SHA256,
                        "inputs_sha256": hashlib.sha256(canonical_inputs).hexdigest(),
                        "target_inventory_sha256": "0" * 64,
                        "target_root": str(self.target_root),
                        "inputs": audit_inputs,
                    },
                    "stages": [
                        {
                            "name": "baseline",
                            "labels": ["codex-config", "codex-rules"]
                            + (["codex-adapter-dba"] if self.reviewer else [])
                            + [
                                "codex-profile-scout",
                                "serena-config",
                                "serena-project-project-one",
                                "serena-project-project-two",
                            ],
                        },
                        {"name": "builder", "labels": ["codex-profile-builder"]},
                        {"name": "operator", "labels": ["codex-profile-operator"]},
                    ],
                    "targets": self.records,
                }
        manifest_payload["audit"]["target_inventory_sha256"] = (
            host_migration_apply._target_inventory_sha256(manifest_payload)
        )
        self.manifest.write_text(
            json.dumps(manifest_payload)
            + "\n",
            encoding="utf-8",
        )
        self.target_audit = self.root / "target-audit.json"
        self.target_audit.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "ok",
                    "errors": [],
                    "warnings": [],
                    "audit": {
                        **{
                            key: value
                            for key, value in manifest_payload["audit"].items()
                            if key != "status"
                        },
                        "codex_version": "0.147.0",
                        "serena_version": "1.6.1",
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.snapshot_dir = self.root / "snapshot"
        present_snapshot = [
            (record["label"], Path(record["path"]))
            for record in self.records
            if record["pre_state"] == "present"
        ]
        absent_snapshot = [
            (record["label"], Path(record["path"]))
            for record in self.records
            if record["pre_state"] == "absent"
        ]
        snapshot(self.snapshot_dir, present_snapshot, absent_snapshot)
        self.receipt = self.snapshot_dir / "receipt.json"
        self.envelopes = {}
        for stage in ("baseline", "builder", "operator"):
            labels = next(
                item["labels"]
                for item in manifest_payload["stages"]
                if item["name"] == stage
            )
            envelope = {
                "schema_version": 1,
                "authorized_stage": stage,
                "authorized_labels": labels,
                "target_root": str(self.target_root),
                "files": {
                    "manifest": file_ref(self.manifest),
                    "target_audit": file_ref(self.target_audit),
                    "snapshot_receipt": file_ref(self.receipt),
                    "policy": file_ref(ROOT / "policies" / "host-policy.json"),
                    "reviewer_routing": file_ref(
                        ROOT
                        / "skills/adversarial-review-loop/references/reviewer-routing.json"
                    ),
                    "python_executable": file_ref(Path(PYTHON)),
                    "codex_executable": file_ref(Path(CODEX)),
                },
                "executor_root": str(ROOT),
                "executors": [
                    {
                        "relative_path": relative,
                        **file_ref(ROOT / relative),
                    }
                    for relative in host_migration_envelope.REQUIRED_RUNTIME_FILES
                ],
                "evidence": {},
                "post_apply": {
                    "audit_command": [
                        PYTHON,
                        "-I",
                        str(ROOT / "scripts" / "validate_host_policy.py"),
                        "--policy",
                        str(ROOT / "policies" / "host-policy.json"),
                        "--reviewer-routing",
                        str(
                            ROOT
                            / "skills/adversarial-review-loop/references/reviewer-routing.json"
                        ),
                        "--codex-config",
                        str(self.first),
                        "--codex-version",
                        "0.147.0",
                        "--codex-profile",
                        "scout={}".format(self.scout_profile),
                        *(
                            [
                                "--codex-profile",
                                "builder={}".format(self.profile),
                            ]
                            if stage == "builder"
                            else (
                                [
                                    "--codex-profile",
                                    "builder={}".format(self.profile),
                                    "--codex-profile",
                                    "operator={}".format(self.operator_profile),
                                ]
                                if stage == "operator"
                                else []
                            )
                        ),
                        "--codex-rules",
                        str(self.second),
                        "--serena-config",
                        str(self.serena_config),
                        "--serena-version",
                        "1.6.1",
                        "--serena-project",
                        "project-one={}".format(
                            self.host / "dev/project-one/.serena/project.yml"
                        ),
                        "--serena-project",
                        "project-two={}".format(
                            self.host / "dev/project-two/.serena/project.yml"
                        ),
                        "--home-root",
                        str(self.host),
                        "--shared-agents-root",
                        str(self.host / ".agents"),
                        "--common-agent-reference-root",
                        str(self.host / ".agents/common-agents"),
                        "--codex-agents-root",
                        str(self.host / ".codex/agents"),
                        "--claude-agents-root",
                        str(self.host / ".claude/agents"),
                        "--audit-scope",
                        "live",
                        "--stage",
                        stage,
                        "--json",
                    ],
                    "runtime_checks": [
                        {
                            "name": name,
                            "command": (
                                [CODEX, "mcp", "list", "--json"]
                                if name == "base"
                                else [
                                    CODEX,
                                    "--profile",
                                    name,
                                    "mcp",
                                    "list",
                                    "--json",
                                ]
                            ),
                            "expectations": expectations,
                        }
                        for name, expectations in host_migration_apply._expected_runtime_checks(
                            self.target_root, manifest_payload, stage
                        ).items()
                    ],
                    "rollback_command": [
                        PYTHON,
                        "-I",
                        str(ROOT / "scripts" / "host_migration_snapshot.py"),
                        "rollback",
                        "--journal",
                        "{journal}",
                        "--confirm-rollback",
                    ],
                },
            }
            envelope_path = self.root / "{}-envelope.json".format(stage)
            envelope_path.write_bytes(host_migration_envelope.canonical_bytes(envelope))
            self.envelopes[stage] = envelope_path

    def _present(self, label, source):
        artifact = self.artifacts[source]
        return {
            "label": label,
            "path": str(source),
            "pre_state": "present",
            "current_mode": "{:04o}".format(source.stat().st_mode & 0o7777),
            "current_sha256": sha256(source),
            "target_mode": "{:04o}".format(artifact.stat().st_mode & 0o7777),
            "target_sha256": sha256(artifact),
        }

    def _absent(self, label, source):
        artifact = self.artifacts[source]
        return {
            "label": label,
            "path": str(source),
            "pre_state": "absent",
            "current_mode": None,
            "current_sha256": None,
            "target_mode": "{:04o}".format(artifact.stat().st_mode & 0o7777),
            "target_sha256": sha256(artifact),
        }

    def apply(self, journal_dir, stage):
        envelope = self.envelopes[stage]
        return host_migration_apply.apply_manifest(
            self.manifest,
            self.target_root,
            self.receipt,
            envelope,
            sha256(envelope),
            sha256(self.manifest),
            journal_dir,
            stage,
        )

    def accept(self, journal_dir, stage):
        envelope = self.envelopes[stage]
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        audit = {
            **manifest["audit"],
            "scope": "live",
            "stage": stage,
        }
        audit_payload = {
            "schema_version": 1,
            "status": "ok",
            "errors": [],
            "warnings": [],
            "audit": audit,
        }
        expected_runtime = host_migration_apply._expected_runtime_checks(
            self.target_root, manifest, stage
        )
        runtime_payloads = [
            [{"name": name, "enabled": enabled} for name, enabled in states.items()]
            for states in expected_runtime.values()
        ]
        with mock.patch.object(
            host_migration_apply,
            "_run_json_command",
            side_effect=[
                (json.dumps(audit_payload).encode("utf-8"), audit_payload),
                *[
                    (json.dumps(payload).encode("utf-8"), payload)
                    for payload in runtime_payloads
                ],
            ],
        ):
            return host_migration_apply.verify_and_accept_stage(
                self.manifest,
                self.target_root,
                self.receipt,
                envelope,
                sha256(envelope),
                sha256(self.manifest),
                journal_dir,
                stage,
            )

    def build_private_envelope(self, stage):
        executor_root = self.root / "executor-{}".format(stage)
        envelope_path = self.root / "private-{}-envelope.json".format(stage)
        host_migration_envelope.build_envelope(
            output_path=envelope_path,
            executor_root=executor_root,
            source_root=ROOT,
            manifest_path=self.manifest,
            target_audit_path=self.target_audit,
            receipt_path=self.receipt,
            target_root=self.target_root,
            stage=stage,
            evidence={},
            audit_command=[
                PYTHON,
                "-I",
                str(executor_root / "scripts/validate_host_policy.py"),
                "--policy",
                str(executor_root / "policies/host-policy.json"),
                "--reviewer-routing",
                str(
                    executor_root
                    / "skills/adversarial-review-loop/references/reviewer-routing.json"
                ),
                "--codex-config",
                str(self.first),
                "--codex-version",
                "0.147.0",
                "--codex-profile",
                "scout={}".format(self.scout_profile),
                *(
                    ["--codex-profile", "builder={}".format(self.profile)]
                    if stage == "builder"
                    else (
                        [
                            "--codex-profile",
                            "builder={}".format(self.profile),
                            "--codex-profile",
                            "operator={}".format(self.operator_profile),
                        ]
                        if stage == "operator"
                        else []
                    )
                ),
                "--codex-rules",
                str(self.second),
                "--serena-config",
                str(self.serena_config),
                "--serena-version",
                "1.6.1",
                "--serena-project",
                "project-one={}".format(
                    self.host / "dev/project-one/.serena/project.yml"
                ),
                "--serena-project",
                "project-two={}".format(
                    self.host / "dev/project-two/.serena/project.yml"
                ),
                "--home-root",
                str(self.host),
                "--shared-agents-root",
                str(self.host / ".agents"),
                "--common-agent-reference-root",
                str(self.host / ".agents/common-agents"),
                "--codex-agents-root",
                str(self.host / ".codex/agents"),
                "--claude-agents-root",
                str(self.host / ".claude/agents"),
                "--audit-scope",
                "live",
                "--stage",
                stage,
                "--json",
            ],
            runtime_checks=[
                {
                    "name": name,
                    "command": (
                        [CODEX, "mcp", "list", "--json"]
                        if name == "base"
                        else [CODEX, "--profile", name, "mcp", "list", "--json"]
                    ),
                    "expectations": expectations,
                }
                for name, expectations in host_migration_apply._expected_runtime_checks(
                    self.target_root,
                    json.loads(self.manifest.read_text(encoding="utf-8")),
                    stage,
                ).items()
            ],
            rollback_command=[
                PYTHON,
                "-I",
                str(executor_root / "scripts/host_migration_snapshot.py"),
                "rollback",
                "--journal",
                "{journal}",
                "--confirm-rollback",
            ],
        )
        return envelope_path, executor_root


class HostMigrationApplyTests(unittest.TestCase):
    def test_apply_uses_one_sealed_manifest_and_receipt_read(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            with mock.patch.object(
                host_migration_apply,
                "load_manifest",
                side_effect=AssertionError("manifest path reopened"),
            ), mock.patch.object(
                host_migration_apply,
                "load_receipt",
                side_effect=AssertionError("receipt path reopened"),
            ):
                payload = fixture.apply(fixture.root / "journal", "baseline")

            self.assertEqual("awaiting_stage_audit", payload["status"])

    def test_apply_refuses_the_host_global_operation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            lock_path = host_migration_apply._host_operation_lock_path(fixture.host)
            with host_migration_apply._OperationLock(lock_path):
                with self.assertRaisesRegex(ValueError, "operation is active"):
                    fixture.apply(journal_dir, "baseline")

            self.assertEqual(
                "current config\n", fixture.first.read_text(encoding="utf-8")
            )

    def test_new_journal_directory_is_fsynced_before_target_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "alternate-journal"
            fsynced = []

            def record_install(_planned, _quarantine):
                self.assertEqual([fixture.root], fsynced)
                return None

            with mock.patch.object(
                host_migration_apply,
                "_fsync_directory",
                side_effect=lambda path: fsynced.append(path),
            ), mock.patch.object(
                host_migration_apply,
                "_install_target",
                side_effect=record_install,
            ):
                fixture.apply(journal_dir, "baseline")

            self.assertEqual([fixture.root], fsynced)

    def test_relative_inputs_are_recorded_as_absolute_for_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            previous = Path.cwd()
            try:
                os.chdir(fixture.root)
                envelope = Path("baseline-envelope.json")
                host_migration_apply.apply_manifest(
                    Path("manifest.json"),
                    Path("target"),
                    Path("snapshot/receipt.json"),
                    envelope,
                    sha256(envelope),
                    sha256(Path("manifest.json")),
                    Path("journal"),
                    "baseline",
                )
                os.chdir(previous)
                result = rollback(fixture.root / "journal" / "journal.json", True)
            finally:
                os.chdir(previous)

            self.assertEqual("rolled_back", result["journal_status"])
            self.assertEqual(
                "current config\n", fixture.first.read_text(encoding="utf-8")
            )

    def test_absent_apply_race_preserves_independent_file(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            record = next(
                item
                for item in fixture.records
                if item["label"] == "codex-profile-builder"
            )
            planned = host_migration_apply.PlannedTarget(
                record=record,
                artifact=fixture.artifacts[fixture.profile],
                data=fixture.artifacts[fixture.profile].read_bytes(),
            )
            original = host_migration_apply._rename_noreplace

            def create_competing_file(source, target):
                if target == fixture.profile:
                    target.write_text("independent user file\n", encoding="utf-8")
                return original(source, target)

            with mock.patch.object(
                host_migration_apply,
                "_rename_noreplace",
                side_effect=create_competing_file,
            ):
                with self.assertRaises(FileExistsError):
                    host_migration_apply._install_target(planned)

            self.assertEqual(
                "independent user file\n",
                fixture.profile.read_text(encoding="utf-8"),
            )

    def test_present_apply_race_retains_competing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            record = fixture.records[0]
            planned = host_migration_apply.PlannedTarget(
                record=record,
                artifact=fixture.artifacts[fixture.first],
                data=fixture.artifacts[fixture.first].read_bytes(),
            )
            original = host_migration_apply._exchange_paths
            injected = False

            def replace_before_exchange(first, second):
                nonlocal injected
                if not injected and second == fixture.first:
                    injected = True
                    second.write_text("independent user file\n", encoding="utf-8")
                    second.chmod(0o600)
                return original(first, second)

            with mock.patch.object(
                host_migration_apply,
                "_exchange_paths",
                side_effect=replace_before_exchange,
            ):
                with self.assertRaisesRegex(ValueError, "approved state"):
                    host_migration_apply._install_target(planned)

            self.assertEqual(
                TARGET_CONFIG,
                fixture.first.read_text(encoding="utf-8"),
            )
            quarantines = list(fixture.first.parent.glob(".host-policy-apply.*"))
            self.assertEqual(1, len(quarantines))
            self.assertEqual(
                "independent user file\n",
                quarantines[0].read_text(encoding="utf-8"),
            )

    def test_present_apply_verification_race_never_deletes_second_competitor(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            record = fixture.records[0]
            planned = host_migration_apply.PlannedTarget(
                record=record,
                artifact=fixture.artifacts[fixture.first],
                data=fixture.artifacts[fixture.first].read_bytes(),
            )
            original = host_migration_apply._verify_path_state

            def replace_installed_target(path, mode, digest, label):
                fixture.first.write_text("second independent file\n", encoding="utf-8")
                fixture.first.chmod(0o600)
                return original(path, mode, digest, label)

            with mock.patch.object(
                host_migration_apply,
                "_verify_path_state",
                side_effect=replace_installed_target,
            ):
                with self.assertRaisesRegex(ValueError, "retained quarantine"):
                    host_migration_apply._install_target(planned)

            self.assertEqual(
                "second independent file\n",
                fixture.first.read_text(encoding="utf-8"),
            )
            quarantines = list(fixture.first.parent.glob(".host-policy-apply.*"))
            self.assertEqual(1, len(quarantines))
            self.assertEqual(
                "current config\n", quarantines[0].read_text(encoding="utf-8")
            )

    def test_dry_run_binds_manifest_snapshot_and_target_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            result = run_cli(
                "--manifest",
                fixture.manifest,
                "--target-root",
                fixture.target_root,
                "--snapshot-receipt",
                fixture.receipt,
                "--stage",
                "baseline",
                "--dry-run",
            )

            self.assertEqual(0, result.returncode, result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual("ok", payload["status"])
            self.assertEqual("dry-run", payload["action"])
            self.assertEqual(
                [
                    "codex-config",
                    "codex-rules",
                    "codex-profile-scout",
                    "serena-config",
                    "serena-project-project-one",
                    "serena-project-project-two",
                ],
                payload["labels"],
            )
            self.assertEqual("current config\n", fixture.first.read_text(encoding="utf-8"))
            self.assertFalse(fixture.profile.exists())

    def test_preflight_rejects_each_drift_before_any_write(self):
        mutations = (
            (
                "source bytes",
                lambda fixture: fixture.first.write_text("drift\n", encoding="utf-8"),
            ),
            ("source mode", lambda fixture: fixture.first.chmod(0o644)),
            (
                "absent became present",
                lambda fixture: fixture.profile.write_text("unexpected\n", encoding="utf-8"),
            ),
            (
                "target bytes",
                lambda fixture: fixture.artifacts[fixture.first].write_text(
                    "tampered\n", encoding="utf-8"
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                fixture = ApplyFixture(directory)
                mutate(fixture)
                before_second = fixture.second.read_bytes()
                stage = "builder" if label == "absent became present" else "baseline"

                with self.assertRaises(ValueError):
                    host_migration_apply.preflight(
                        fixture.manifest,
                        fixture.target_root,
                        fixture.receipt,
                        stage,
                    )

                self.assertEqual(before_second, fixture.second.read_bytes())

    def test_preflight_rejects_symlink_target_and_manifest_inventory_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            actual = fixture.host / "actual.toml"
            fixture.first.replace(actual)
            fixture.first.symlink_to(actual)

            with self.assertRaises(ValueError):
                host_migration_apply.preflight(
                    fixture.manifest,
                    fixture.target_root,
                    fixture.receipt,
                    "baseline",
                )

        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            manifest["targets"].pop()
            fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaises(ValueError):
                host_migration_apply.preflight(
                    fixture.manifest,
                    fixture.target_root,
                    fixture.receipt,
                    "baseline",
                )

    def test_confirmed_apply_requires_the_exact_approval_envelope_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            result = run_cli(
                "--manifest",
                fixture.manifest,
                "--target-root",
                fixture.target_root,
                "--snapshot-receipt",
                fixture.receipt,
                "--journal-dir",
                journal_dir,
                "--stage",
                "baseline",
                "--confirm-apply",
            )

            self.assertEqual(1, result.returncode, result.stdout)
            self.assertIn("requires approval envelope", result.stdout)
            self.assertEqual("current config\n", fixture.first.read_text(encoding="utf-8"))
            self.assertFalse(fixture.profile.exists())
            self.assertFalse(journal_dir.exists())

    def test_envelope_verify_cli_checks_the_full_cross_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            envelope, _ = fixture.build_private_envelope("baseline")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ENVELOPE_SCRIPT),
                    "verify",
                    "--envelope",
                    str(envelope),
                    "--expected-envelope-sha256",
                    sha256(envelope),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stdout)
            self.assertEqual("ok", json.loads(result.stdout)["status"])

            payload = json.loads(envelope.read_text(encoding="utf-8"))
            command = payload["post_apply"]["audit_command"]
            command[command.index("--codex-config") + 1] = str(
                fixture.root / "decoy-config.toml"
            )
            envelope.write_bytes(host_migration_envelope.canonical_bytes(payload))
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ENVELOPE_SCRIPT),
                    "verify",
                    "--envelope",
                    str(envelope),
                    "--expected-envelope-sha256",
                    sha256(envelope),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(1, result.returncode, result.stdout)
            self.assertIn("--codex-config differs", result.stdout)

    def test_envelope_build_and_verify_reject_strict_manifest_violations(self):
        def mutate_manifest(fixture, case):
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            duplicate = dict(manifest["targets"][0])
            if case == "duplicate-label":
                manifest["targets"].append(duplicate)
            elif case == "duplicate-path":
                duplicate["label"] = "codex-profile-extra"
                manifest["targets"].append(duplicate)
                manifest["stages"][0]["labels"].append(duplicate["label"])
            else:
                duplicate.update(
                    {
                        "label": "extra-target",
                        "path": str(fixture.host / ".codex" / "extra.toml"),
                        "pre_state": "absent",
                        "current_mode": None,
                        "current_sha256": None,
                    }
                )
                manifest["targets"].append(duplicate)
                manifest["stages"][0]["labels"].append(duplicate["label"])
            manifest["audit"]["target_inventory_sha256"] = (
                host_migration_apply._target_inventory_sha256(manifest)
            )
            fixture.manifest.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            target_audit = json.loads(
                fixture.target_audit.read_text(encoding="utf-8")
            )
            target_audit["audit"].update(
                {
                    key: value
                    for key, value in manifest["audit"].items()
                    if key != "status"
                }
            )
            fixture.target_audit.write_text(
                json.dumps(target_audit) + "\n", encoding="utf-8"
            )

        for case in ("duplicate-label", "duplicate-path", "extra-target"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                fixture = ApplyFixture(directory)
                envelope, executor = fixture.build_private_envelope("baseline")
                mutate_manifest(fixture, case)

                verify_payload = json.loads(envelope.read_text(encoding="utf-8"))
                verify_payload["files"]["manifest"] = file_ref(fixture.manifest)
                verify_payload["files"]["target_audit"] = file_ref(
                    fixture.target_audit
                )
                envelope.write_bytes(
                    host_migration_envelope.canonical_bytes(verify_payload)
                )
                verify_result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(executor / "scripts/host_migration_envelope.py"),
                        "verify",
                        "--envelope",
                        str(envelope),
                        "--expected-envelope-sha256",
                        sha256(envelope),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(1, verify_result.returncode, verify_result.stdout)
                self.assertEqual("error", json.loads(verify_result.stdout)["status"])

                build_executor = fixture.root / "build-executor"
                audit_command = list(
                    verify_payload["post_apply"]["audit_command"]
                )
                rollback_command = list(
                    verify_payload["post_apply"]["rollback_command"]
                )
                replacements = {
                    str(executor / "scripts/validate_host_policy.py"): str(
                        build_executor / "scripts/validate_host_policy.py"
                    ),
                    str(executor / "policies/host-policy.json"): str(
                        build_executor / "policies/host-policy.json"
                    ),
                    str(
                        executor
                        / "skills/adversarial-review-loop/references/reviewer-routing.json"
                    ): str(
                        build_executor
                        / "skills/adversarial-review-loop/references/reviewer-routing.json"
                    ),
                    str(executor / "scripts/host_migration_snapshot.py"): str(
                        build_executor / "scripts/host_migration_snapshot.py"
                    ),
                }
                audit_command = [replacements.get(value, value) for value in audit_command]
                rollback_command = [
                    replacements.get(value, value) for value in rollback_command
                ]
                spec = fixture.root / "build-spec.json"
                spec.write_text(
                    json.dumps(
                        {
                            "source_root": str(ROOT),
                            "manifest": str(fixture.manifest),
                            "target_audit": str(fixture.target_audit),
                            "snapshot_receipt": str(fixture.receipt),
                            "target_root": str(fixture.target_root),
                            "stage": "baseline",
                            "evidence": {},
                            "audit_command": audit_command,
                            "runtime_checks": verify_payload["post_apply"][
                                "runtime_checks"
                            ],
                            "rollback_command": rollback_command,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                build_result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(ENVELOPE_SCRIPT),
                        "build",
                        "--spec",
                        str(spec),
                        "--output",
                        str(fixture.root / "rejected-envelope.json"),
                        "--executor-root",
                        str(build_executor),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(1, build_result.returncode, build_result.stdout)
                self.assertEqual("error", json.loads(build_result.stdout)["status"])
                self.assertFalse((fixture.root / "rejected-envelope.json").exists())
                self.assertFalse(build_executor.exists())

    def test_copied_executor_ignores_hostile_pythonpath_for_verify_apply_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            envelope, executor = fixture.build_private_envelope("baseline")
            hostile = fixture.root / "hostile"
            hostile_package = hostile / "scripts"
            hostile_package.mkdir(parents=True)
            marker = fixture.root / "external-package-executed"
            marker_code = (
                "from pathlib import Path\n"
                "Path({!r}).write_text('executed', encoding='utf-8')\n".format(
                    str(marker)
                )
            )
            (hostile_package / "__init__.py").write_text(
                marker_code, encoding="utf-8"
            )
            for name in (
                "host_migration_apply.py",
                "host_migration_envelope.py",
                "host_migration_snapshot.py",
                "agent_contracts.py",
            ):
                (hostile_package / name).write_text(marker_code, encoding="utf-8")
            hostile_environment = dict(os.environ)
            hostile_environment["PYTHONPATH"] = str(hostile)
            isolated_home = fixture.root / "isolated-home"
            user_site = (
                isolated_home
                / "Library/Python/3.9/lib/python/site-packages"
            )
            user_site.mkdir(parents=True)
            (user_site / "sitecustomize.py").write_text(
                marker_code, encoding="utf-8"
            )
            hostile_environment["HOME"] = str(isolated_home)

            validator_help = subprocess.run(
                [
                    PYTHON,
                    "-I",
                    str(executor / "scripts/validate_host_policy.py"),
                    "--help",
                ],
                env=hostile_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, validator_help.returncode, validator_help.stdout)

            valid_verify = subprocess.run(
                [
                    PYTHON,
                    "-I",
                    str(executor / "scripts/host_migration_envelope.py"),
                    "verify",
                    "--envelope",
                    str(envelope),
                    "--expected-envelope-sha256",
                    sha256(envelope),
                ],
                env=hostile_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, valid_verify.returncode, valid_verify.stdout)

            dry_run = subprocess.run(
                [
                    PYTHON,
                    "-I",
                    str(executor / "scripts/host_migration_apply.py"),
                    "--manifest",
                    str(fixture.manifest),
                    "--target-root",
                    str(fixture.target_root),
                    "--snapshot-receipt",
                    str(fixture.receipt),
                    "--stage",
                    "baseline",
                    "--dry-run",
                ],
                env=hostile_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, dry_run.returncode, dry_run.stdout)

            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            journal_path = journal_dir / "journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["stage_envelopes"]["baseline"] = sha256(envelope)
            journal["stage_envelope_paths"]["baseline"] = str(envelope)
            journal["pending_audit_envelope_sha256"] = sha256(envelope)
            journal_path.write_text(json.dumps(journal) + "\n", encoding="utf-8")
            restored = subprocess.run(
                [
                    PYTHON,
                    "-I",
                    str(executor / "scripts/host_migration_snapshot.py"),
                    "rollback",
                    "--journal",
                    str(journal_dir / "journal.json"),
                    "--confirm-rollback",
                ],
                env=hostile_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, restored.returncode, restored.stdout)

            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            manifest["targets"].append(dict(manifest["targets"][0]))
            manifest["audit"]["target_inventory_sha256"] = (
                host_migration_apply._target_inventory_sha256(manifest)
            )
            fixture.manifest.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            target_audit = json.loads(
                fixture.target_audit.read_text(encoding="utf-8")
            )
            target_audit["audit"].update(
                {
                    key: value
                    for key, value in manifest["audit"].items()
                    if key != "status"
                }
            )
            fixture.target_audit.write_text(
                json.dumps(target_audit) + "\n", encoding="utf-8"
            )
            malformed_envelope = json.loads(envelope.read_text(encoding="utf-8"))
            malformed_envelope["files"]["manifest"] = file_ref(fixture.manifest)
            malformed_envelope["files"]["target_audit"] = file_ref(
                fixture.target_audit
            )
            envelope.write_bytes(
                host_migration_envelope.canonical_bytes(malformed_envelope)
            )
            rejected = subprocess.run(
                [
                    PYTHON,
                    "-I",
                    str(executor / "scripts/host_migration_envelope.py"),
                    "verify",
                    "--envelope",
                    str(envelope),
                    "--expected-envelope-sha256",
                    sha256(envelope),
                ],
                env=hostile_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(1, rejected.returncode, rejected.stdout)
            self.assertFalse(marker.exists())

    def test_approved_executables_require_protected_ownership_and_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "tool"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            self.assertRegex(
                host_migration_envelope._executable_reference(
                    Path("/usr/bin/python3"), "system Python"
                )["sha256"],
                r"^[0-9a-f]{64}$",
            )

            executable.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "non-writable by group/other"):
                host_migration_envelope._executable_reference(
                    executable, "writable executable"
                )

            executable.chmod(0o755)
            root.chmod(0o777)
            try:
                with self.assertRaisesRegex(
                    ValueError, "ancestry must be owner-controlled"
                ):
                    host_migration_envelope._executable_reference(
                        executable, "writable ancestry executable"
                    )
            finally:
                root.chmod(0o700)

    def test_envelope_build_requires_a_private_approval_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            public = fixture.root / "public-proposal"
            public.mkdir(mode=0o755)
            public.chmod(0o755)

            with self.assertRaisesRegex(ValueError, "mode 0700"):
                host_migration_envelope.build_envelope(
                    output_path=public / "approval-envelope.json",
                    executor_root=public / "executor",
                    source_root=ROOT,
                    manifest_path=fixture.manifest,
                    target_audit_path=fixture.target_audit,
                    receipt_path=fixture.receipt,
                    target_root=fixture.target_root,
                    stage="baseline",
                    evidence={},
                    audit_command=json.loads(
                        fixture.envelopes["baseline"].read_text(encoding="utf-8")
                    )["post_apply"]["audit_command"],
                    runtime_checks=json.loads(
                        fixture.envelopes["baseline"].read_text(encoding="utf-8")
                    )["post_apply"]["runtime_checks"],
                    rollback_command=[
                        sys.executable,
                        "-I",
                        str(public / "executor/scripts/host_migration_snapshot.py"),
                        "rollback",
                        "--journal",
                        "{journal}",
                        "--confirm-rollback",
                    ],
                )

            self.assertFalse((public / "approval-envelope.json").exists())
            self.assertFalse((public / "executor").exists())

    def test_envelope_build_fsyncs_intermediate_executor_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            fsynced = []
            original = host_migration_envelope._fsync_directory

            def record(path):
                fsynced.append(path)
                return original(path)

            with mock.patch.object(
                host_migration_envelope, "_fsync_directory", side_effect=record
            ):
                _, executor = fixture.build_private_envelope("baseline")

            self.assertIn(executor / "vendor", fsynced)
            self.assertIn(executor / "vendor/py39", fsynced)
            self.assertIn(executor / "vendor/licenses", fsynced)

    def test_envelope_build_cli_reports_malformed_spec_as_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            spec = root / "spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "source_root": 1,
                        "manifest": 2,
                        "target_audit": 3,
                        "snapshot_receipt": 4,
                        "target_root": 5,
                        "stage": 6,
                        "evidence": {},
                        "audit_command": [],
                        "runtime_checks": [],
                        "rollback_command": [],
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ENVELOPE_SCRIPT),
                    "build",
                    "--spec",
                    str(spec),
                    "--output",
                    str(root / "approval-envelope.json"),
                    "--executor-root",
                    str(root / "executor"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(1, result.returncode, result.stdout)
            self.assertEqual("error", json.loads(result.stdout)["status"])
            self.assertFalse((root / "executor").exists())

    def test_stage_stays_rollback_only_until_bound_audit_and_runtime_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            journal = fixture.apply(journal_dir, "baseline")

            self.assertEqual("awaiting_stage_audit", journal["status"])
            self.assertTrue(journal["rollback_required"])
            with self.assertRaisesRegex(ValueError, "must be rolled back"):
                fixture.apply(journal_dir, "builder")

            accepted = fixture.accept(journal_dir, "baseline")
            self.assertEqual("ready", accepted["status"])
            self.assertFalse(accepted["rollback_required"])
            self.assertIn("baseline", accepted["stage_audits"])
            audit_record = accepted["stage_audits"]["baseline"]
            for key, digest_key in (
                ("audit_file", "audit_sha256"),
                ("runtime_file", "runtime_sha256"),
            ):
                receipt = journal_dir / audit_record[key]
                self.assertEqual(0o600, receipt.stat().st_mode & 0o777)
                self.assertEqual(audit_record[digest_key], sha256(receipt))
            self.assertEqual(0o700, journal_dir.stat().st_mode & 0o777)
            self.assertEqual(
                0o600, (journal_dir / "journal.json").stat().st_mode & 0o777
            )

    def test_missing_installed_profile_runtime_check_fails_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            source = fixture.envelopes["baseline"]
            envelope = json.loads(source.read_text(encoding="utf-8"))
            envelope["post_apply"]["runtime_checks"] = envelope["post_apply"][
                "runtime_checks"
            ][:1]
            narrowed = fixture.root / "narrowed-envelope.json"
            narrowed.write_bytes(host_migration_envelope.canonical_bytes(envelope))

            with self.assertRaisesRegex(ValueError, "runtime checks differ"):
                host_migration_apply.apply_manifest(
                    fixture.manifest,
                    fixture.target_root,
                    fixture.receipt,
                    narrowed,
                    sha256(narrowed),
                    sha256(fixture.manifest),
                    fixture.root / "journal",
                    "baseline",
                )

            self.assertEqual(
                "current config\n", fixture.first.read_text(encoding="utf-8")
            )
            self.assertFalse((fixture.root / "journal").exists())

    def test_incomplete_live_audit_command_fails_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            source = fixture.envelopes["baseline"]
            envelope = json.loads(source.read_text(encoding="utf-8"))
            command = envelope["post_apply"]["audit_command"]
            version_index = command.index("--serena-version")
            del command[version_index : version_index + 2]
            incomplete = fixture.root / "incomplete-envelope.json"
            incomplete.write_bytes(host_migration_envelope.canonical_bytes(envelope))

            with self.assertRaisesRegex(ValueError, "--serena-version differs"):
                host_migration_apply.apply_manifest(
                    fixture.manifest,
                    fixture.target_root,
                    fixture.receipt,
                    incomplete,
                    sha256(incomplete),
                    sha256(fixture.manifest),
                    fixture.root / "journal",
                    "baseline",
                )

            self.assertEqual(
                "current config\n", fixture.first.read_text(encoding="utf-8")
            )
            self.assertFalse((fixture.root / "journal").exists())

    def test_unapproved_command_executable_fails_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            envelope = json.loads(
                fixture.envelopes["baseline"].read_text(encoding="utf-8")
            )
            substitute = str(fixture.root / "missing-python")
            envelope["files"]["python_executable"]["path"] = substitute
            envelope["post_apply"]["audit_command"][0] = substitute
            envelope["post_apply"]["rollback_command"][0] = substitute
            changed = fixture.root / "substitute-executable-envelope.json"
            changed.write_bytes(host_migration_envelope.canonical_bytes(envelope))

            with self.assertRaisesRegex(ValueError, "regular non-symlink file"):
                host_migration_apply.apply_manifest(
                    fixture.manifest,
                    fixture.target_root,
                    fixture.receipt,
                    changed,
                    sha256(changed),
                    sha256(fixture.manifest),
                    fixture.root / "journal",
                    "baseline",
                )

            self.assertEqual(
                "current config\n", fixture.first.read_text(encoding="utf-8")
            )
            self.assertFalse((fixture.root / "journal").exists())

    def test_stage_receipts_are_retry_safe_if_final_journal_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            original = host_migration_apply._write_journal

            def fail_ready(path, payload):
                if payload["status"] == "ready":
                    raise OSError("injected final journal failure")
                return original(path, payload)

            with mock.patch.object(
                host_migration_apply, "_write_journal", side_effect=fail_ready
            ):
                with self.assertRaisesRegex(OSError, "final journal failure"):
                    fixture.accept(journal_dir, "baseline")

            journal = json.loads(
                (journal_dir / "journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual("awaiting_stage_audit", journal["status"])
            self.assertTrue((journal_dir / "baseline-live-audit.json").is_file())
            self.assertTrue((journal_dir / "baseline-runtime-mcp.json").is_file())

            accepted = fixture.accept(journal_dir, "baseline")
            self.assertEqual("ready", accepted["status"])

    def test_baseline_runtime_calls_use_literal_inventory_and_approved_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            envelope_path = fixture.envelopes["baseline"]
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [
                    {
                        "name": "base",
                        "command": [CODEX, "mcp", "list", "--json"],
                        "expectations": {"db-mcp": False},
                    },
                    {
                        "name": "scout",
                        "command": [
                            CODEX,
                            "--profile",
                            "scout",
                            "mcp",
                            "list",
                            "--json",
                        ],
                        "expectations": {"db-mcp": False},
                    },
                ],
                envelope["post_apply"]["runtime_checks"],
            )
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            audit_payload = {
                "status": "ok",
                "errors": [],
                "warnings": [],
                "audit": {
                    **manifest["audit"],
                    "scope": "live",
                    "stage": "baseline",
                },
            }
            runtime_payload = [{"name": "db-mcp", "enabled": False}]
            calls = []

            def probe(argv, label, environment_overrides=None):
                calls.append((argv, label, environment_overrides))
                payload = (
                    audit_payload if label == "live host-policy audit" else runtime_payload
                )
                return json.dumps(payload).encode("utf-8"), payload

            with mock.patch.object(
                host_migration_apply, "_run_json_command", side_effect=probe
            ):
                host_migration_apply.verify_and_accept_stage(
                    fixture.manifest,
                    fixture.target_root,
                    fixture.receipt,
                    envelope_path,
                    sha256(envelope_path),
                    sha256(fixture.manifest),
                    journal_dir,
                    "baseline",
                )

            self.assertEqual(
                [
                    envelope["post_apply"]["audit_command"],
                    [CODEX, "mcp", "list", "--json"],
                    [CODEX, "--profile", "scout", "mcp", "list", "--json"],
                ],
                [call[0] for call in calls],
            )
            approved_environment = {
                "HOME": str(fixture.host),
                "CODEX_HOME": str(fixture.codex),
                "TMPDIR": str(journal_dir),
            }
            self.assertEqual(
                [approved_environment, approved_environment, approved_environment],
                [call[2] for call in calls],
            )

    def test_operator_runtime_calls_use_every_profile_in_literal_order(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            fixture.accept(journal_dir, "baseline")
            fixture.apply(journal_dir, "builder")
            fixture.accept(journal_dir, "builder")
            fixture.apply(journal_dir, "operator")
            envelope_path = fixture.envelopes["operator"]
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            audit_payload = {
                "status": "ok",
                "errors": [],
                "warnings": [],
                "audit": {
                    **manifest["audit"],
                    "scope": "live",
                    "stage": "operator",
                },
            }
            runtime_payload = [{"name": "db-mcp", "enabled": False}]
            calls = []

            def probe(argv, label, environment_overrides=None):
                calls.append((argv, label, environment_overrides))
                payload = (
                    audit_payload if label == "live host-policy audit" else runtime_payload
                )
                return json.dumps(payload).encode("utf-8"), payload

            with mock.patch.object(
                host_migration_apply, "_run_json_command", side_effect=probe
            ):
                host_migration_apply.verify_and_accept_stage(
                    fixture.manifest,
                    fixture.target_root,
                    fixture.receipt,
                    envelope_path,
                    sha256(envelope_path),
                    sha256(fixture.manifest),
                    journal_dir,
                    "operator",
                )

            expected_commands = [
                envelope["post_apply"]["audit_command"],
                [CODEX, "mcp", "list", "--json"],
                [CODEX, "--profile", "scout", "mcp", "list", "--json"],
                [CODEX, "--profile", "builder", "mcp", "list", "--json"],
                [CODEX, "--profile", "operator", "mcp", "list", "--json"],
            ]
            self.assertEqual(expected_commands, [call[0] for call in calls])
            approved_environment = {
                "HOME": str(fixture.host),
                "CODEX_HOME": str(fixture.codex),
                "TMPDIR": str(journal_dir),
            }
            self.assertEqual(
                [approved_environment] * len(expected_commands),
                [call[2] for call in calls],
            )

    def test_next_stage_rejects_tampered_prior_stage_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            fixture.accept(journal_dir, "baseline")
            receipt = journal_dir / "baseline-live-audit.json"
            receipt.write_text('{"status":"tampered"}\n', encoding="utf-8")
            receipt.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "differs from journal"):
                fixture.apply(journal_dir, "builder")

            self.assertFalse(fixture.profile.exists())

    def test_exact_manifest_and_external_target_audit_drift_fail_before_writes(self):
        for label in ("manifest", "target audit"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                fixture = ApplyFixture(directory)
                envelope = fixture.envelopes["baseline"]
                expected_manifest = sha256(fixture.manifest)
                expected_envelope = sha256(envelope)
                if label == "manifest":
                    fixture.manifest.write_text(
                        fixture.manifest.read_text(encoding="utf-8") + " ",
                        encoding="utf-8",
                    )
                else:
                    fixture.target_audit.write_text(
                        fixture.target_audit.read_text(encoding="utf-8") + " ",
                        encoding="utf-8",
                    )

                with self.assertRaises(ValueError):
                    host_migration_apply.apply_manifest(
                        fixture.manifest,
                        fixture.target_root,
                        fixture.receipt,
                        envelope,
                        expected_envelope,
                        expected_manifest,
                        fixture.root / "journal",
                        "baseline",
                    )

                self.assertEqual(
                    "current config\n", fixture.first.read_text(encoding="utf-8")
                )
                self.assertFalse((fixture.root / "journal").exists())

    def test_baseline_rejects_future_stage_drift_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            fixture.profile.write_text("independent profile\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "expected target to remain absent"):
                fixture.apply(fixture.root / "journal", "baseline")

            self.assertEqual("current config\n", fixture.first.read_text(encoding="utf-8"))
            self.assertFalse((fixture.root / "journal").exists())

    def test_baseline_envelope_cannot_authorize_a_later_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            envelope = fixture.envelopes["baseline"]

            with self.assertRaisesRegex(ValueError, "does not authorize"):
                host_migration_apply.apply_manifest(
                    fixture.manifest,
                    fixture.target_root,
                    fixture.receipt,
                    envelope,
                    sha256(envelope),
                    sha256(fixture.manifest),
                    fixture.root / "journal",
                    "builder",
                )

            self.assertFalse((fixture.root / "journal").exists())

    def test_private_executor_drift_is_rejected_before_host_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            envelope, executor_root = fixture.build_private_envelope("baseline")
            validator = executor_root / "scripts/validate_host_policy.py"
            validator.chmod(0o600)
            validator.write_text(
                validator.read_text(encoding="utf-8") + "\n# drift\n",
                encoding="utf-8",
            )
            command = [
                sys.executable,
                "-I",
                str(executor_root / "scripts/host_migration_apply.py"),
                "--manifest",
                str(fixture.manifest),
                "--target-root",
                str(fixture.target_root),
                "--snapshot-receipt",
                str(fixture.receipt),
                "--approval-envelope",
                str(envelope),
                "--expected-envelope-sha256",
                sha256(envelope),
                "--expected-manifest-sha256",
                sha256(fixture.manifest),
                "--journal-dir",
                str(fixture.root / "journal"),
                "--stage",
                "baseline",
                "--confirm-apply",
            ]
            result = subprocess.run(
                command,
                cwd=fixture.root,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(1, result.returncode, result.stdout)
            self.assertIn("differs from the approval envelope", result.stdout)
            self.assertEqual("current config\n", fixture.first.read_text(encoding="utf-8"))
            self.assertFalse((fixture.root / "journal").exists())

    def test_vendored_dependency_drift_is_rejected_by_sealed_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            envelope, executor_root = fixture.build_private_envelope("baseline")
            vendored_parser = executor_root / "vendor/py39/tomli/_parser.py"
            marker = fixture.root / "tampered-vendor-executed"
            vendored_parser.chmod(0o600)
            vendored_parser.write_text(
                vendored_parser.read_text(encoding="utf-8")
                + "\nfrom pathlib import Path\n"
                + "Path({!r}).write_text('executed', encoding='utf-8')\n".format(
                    str(marker)
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    PYTHON,
                    "-I",
                    str(executor_root / "scripts/host_migration_envelope.py"),
                    "verify",
                    "--envelope",
                    str(envelope),
                    "--expected-envelope-sha256",
                    sha256(envelope),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(1, result.returncode, result.stdout)
            self.assertIn("differs from the approval envelope", result.stdout)
            self.assertFalse(marker.exists())

    def test_copied_core_drift_is_rejected_before_import_for_verify_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            envelope, executor_root = fixture.build_private_envelope("baseline")
            copied_apply = executor_root / "scripts/host_migration_apply.py"
            marker = fixture.root / "tampered-core-executed"

            fixture.apply(fixture.root / "journal", "baseline")
            journal_path = fixture.root / "journal/journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["stage_envelopes"]["baseline"] = sha256(envelope)
            journal["stage_envelope_paths"]["baseline"] = str(envelope)
            journal_path.write_text(json.dumps(journal) + "\n", encoding="utf-8")

            copied_apply.chmod(0o600)
            copied_apply.write_text(
                copied_apply.read_text(encoding="utf-8")
                + "\nfrom pathlib import Path\n"
                + "Path({!r}).write_text('executed', encoding='utf-8')\n".format(
                    str(marker)
                ),
                encoding="utf-8",
            )
            copied_apply.chmod(0o500)

            commands = (
                [
                    PYTHON,
                    "-I",
                    str(executor_root / "scripts/host_migration_envelope.py"),
                    "verify",
                    "--envelope",
                    str(envelope),
                    "--expected-envelope-sha256",
                    sha256(envelope),
                ],
                [
                    PYTHON,
                    "-I",
                    str(executor_root / "scripts/host_migration_snapshot.py"),
                    "rollback",
                    "--journal",
                    str(journal_path),
                    "--confirm-rollback",
                ],
            )
            for command in commands:
                with self.subTest(command=command[2]):
                    result = subprocess.run(
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(1, result.returncode, result.stdout)
                    self.assertFalse(marker.exists())

            self.assertEqual(TARGET_CONFIG, fixture.first.read_text(encoding="utf-8"))

    def test_vendored_dependency_permissions_fail_before_validator_import(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            _, executor_root = fixture.build_private_envelope("baseline")
            vendored_parser = executor_root / "vendor/py39/tomli/_parser.py"
            vendored_parser.chmod(0o777)

            result = subprocess.run(
                [
                    PYTHON,
                    "-I",
                    str(executor_root / "scripts/validate_host_policy.py"),
                    "--help",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(1, result.returncode, result.stdout)
            self.assertEqual("", result.stderr)
            self.assertIn("not owner-controlled", result.stdout)

    def test_one_shot_cli_rolls_back_when_live_audit_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            envelope, executor_root = fixture.build_private_envelope("baseline")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(executor_root / "scripts/host_migration_apply.py"),
                    "--manifest",
                    str(fixture.manifest),
                    "--target-root",
                    str(fixture.target_root),
                    "--snapshot-receipt",
                    str(fixture.receipt),
                    "--approval-envelope",
                    str(envelope),
                    "--expected-envelope-sha256",
                    sha256(envelope),
                    "--expected-manifest-sha256",
                    sha256(fixture.manifest),
                    "--journal-dir",
                    str(fixture.root / "journal"),
                    "--stage",
                    "baseline",
                    "--confirm-apply",
                ],
                cwd=fixture.root,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(1, result.returncode, result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual("ok", payload["recovery"]["status"])
            self.assertEqual("current config\n", fixture.first.read_text(encoding="utf-8"))
            journal = json.loads(
                (fixture.root / "journal/journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual("rolled_back", journal["status"])

    def test_next_stage_prewrite_error_does_not_roll_back_ready_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            fixture.accept(journal_dir, "baseline")
            journal_path = journal_dir / "journal.json"
            before_journal = journal_path.read_bytes()
            before_config = fixture.first.read_bytes()
            envelope = fixture.envelopes["builder"]

            result = run_cli(
                "--manifest",
                fixture.manifest,
                "--target-root",
                fixture.target_root,
                "--snapshot-receipt",
                fixture.receipt,
                "--approval-envelope",
                envelope,
                "--expected-envelope-sha256",
                "0" * 64,
                "--expected-manifest-sha256",
                sha256(fixture.manifest),
                "--journal-dir",
                journal_dir,
                "--stage",
                "builder",
                "--confirm-apply",
            )

            self.assertEqual(1, result.returncode, result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["recovery"]["attempted"])
            self.assertEqual(before_journal, journal_path.read_bytes())
            self.assertEqual(before_config, fixture.first.read_bytes())
            self.assertFalse(fixture.profile.exists())

    def test_first_next_stage_intent_journal_failure_preserves_ready_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            fixture.accept(journal_dir, "baseline")
            journal_path = journal_dir / "journal.json"
            before_journal = journal_path.read_bytes()
            before_config = fixture.first.read_bytes()
            original_write = host_migration_apply._write_journal
            injected = False

            def fail_first_builder_intent(path, payload):
                nonlocal injected
                if (
                    not injected
                    and payload.get("status") == "applying"
                    and payload.get("active_stage") == "builder"
                    and payload.get("active_label") == "codex-profile-builder"
                ):
                    injected = True
                    raise OSError("injected builder intent failure")
                return original_write(path, payload)

            envelope = fixture.envelopes["builder"]
            output = io.StringIO()
            with mock.patch.object(
                host_migration_apply,
                "_write_journal",
                side_effect=fail_first_builder_intent,
            ), redirect_stdout(output):
                exit_code = host_migration_apply.main(
                    [
                        "--manifest",
                        str(fixture.manifest),
                        "--target-root",
                        str(fixture.target_root),
                        "--snapshot-receipt",
                        str(fixture.receipt),
                        "--approval-envelope",
                        str(envelope),
                        "--expected-envelope-sha256",
                        sha256(envelope),
                        "--expected-manifest-sha256",
                        sha256(fixture.manifest),
                        "--journal-dir",
                        str(journal_dir),
                        "--stage",
                        "builder",
                        "--confirm-apply",
                    ]
                )

            self.assertEqual(1, exit_code)
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["recovery"]["attempted"])
            self.assertEqual(before_journal, journal_path.read_bytes())
            self.assertEqual(before_config, fixture.first.read_bytes())
            self.assertFalse(fixture.profile.exists())

    def test_json_probe_forces_approved_home_and_bounds_error_output(self):
        output, payload = host_migration_apply._run_json_command(
            [
                sys.executable,
                "-c",
                (
                    "import json,os;"
                    "print(json.dumps({'home':os.environ['HOME'],"
                    "'codex_home':os.environ['CODEX_HOME']}))"
                ),
            ],
            "environment probe",
            environment_overrides={
                "HOME": "/approved/home",
                "CODEX_HOME": "/approved/home/.codex",
            },
        )
        self.assertTrue(output)
        self.assertEqual(
            {"home": "/approved/home", "codex_home": "/approved/home/.codex"},
            payload,
        )

        with self.assertRaisesRegex(ValueError, "too much error output"):
            host_migration_apply._run_json_command(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.stderr.write('x'*65537)",
                ],
                "bounded probe",
            )

        marker = "must-not-leak-from-stderr"
        with self.assertRaises(ValueError) as raised:
            host_migration_apply._run_json_command(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.stderr.write(sys.argv[1]);sys.exit(2)",
                    marker,
                ],
                "redacted probe",
            )
        self.assertNotIn(marker, str(raised.exception))
        self.assertIn("error output bytes=", str(raised.exception))

        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "synthetic-secret", "DYLD_INSERT_LIBRARIES": "bad"},
        ):
            _, sanitized = host_migration_apply._run_json_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json,os;print(json.dumps({"
                        "'api': 'OPENAI_API_KEY' in os.environ,"
                        "'loader': 'DYLD_INSERT_LIBRARIES' in os.environ}))"
                    ),
                ],
                "sanitized environment probe",
            )
        self.assertEqual({"api": False, "loader": False}, sanitized)

    def test_json_probe_kills_descendants_after_leader_exit_on_output_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            group_file = Path(directory) / "group-id"
            child_code = (
                "import os,sys,time;"
                "open(sys.argv[1],'a').write(str(os.getpid())+'\\n');"
                "sys.stderr.write('x'*70000);sys.stderr.flush();time.sleep(30)"
            )
            parent_code = (
                "import os,subprocess,sys;"
                "open(sys.argv[1],'w').write(str(os.getpid())+'\\n');"
                "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]])"
            )
            with self.assertRaisesRegex(ValueError, "too much error output"):
                host_migration_apply._run_json_command(
                    [
                        sys.executable,
                        "-c",
                        parent_code,
                        str(group_file),
                        child_code,
                    ],
                    "descendant probe",
                )
            identifiers = [
                int(value)
                for value in group_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(2, len(identifiers))
            child_pid = identifiers[1]
            child_alive = True
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(child_pid)],
                    check=False,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if not status or status.startswith("Z"):
                    child_alive = False
                    break
                time.sleep(0.02)
            self.assertFalse(child_alive)

    def test_ready_transition_rechecks_live_state_after_runtime_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            audit_payload = {
                "status": "ok",
                "errors": [],
                "warnings": [],
                "audit": {
                    **manifest["audit"],
                    "scope": "live",
                    "stage": "baseline",
                },
            }
            runtime_payload = [{"name": "db-mcp", "enabled": False}]

            def run_probe(_argv, label, environment_overrides=None):
                if label.startswith("fresh Codex MCP inventory"):
                    if label.endswith("scout"):
                        fixture.first.write_text(
                            "non-cooperating drift\n", encoding="utf-8"
                        )
                        fixture.first.chmod(0o600)
                    return json.dumps(runtime_payload).encode("utf-8"), runtime_payload
                return json.dumps(audit_payload).encode("utf-8"), audit_payload

            envelope = fixture.envelopes["baseline"]
            with mock.patch.object(
                host_migration_apply, "_run_json_command", side_effect=run_probe
            ):
                with self.assertRaisesRegex(
                    ValueError, "post-apply target verification failed"
                ):
                    host_migration_apply.verify_and_accept_stage(
                        fixture.manifest,
                        fixture.target_root,
                        fixture.receipt,
                        envelope,
                        sha256(envelope),
                        sha256(fixture.manifest),
                        journal_dir,
                        "baseline",
                    )

            journal = json.loads(
                (journal_dir / "journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual("awaiting_stage_audit", journal["status"])
            self.assertTrue(journal["rollback_required"])

    def test_builder_runtime_mismatch_triggers_full_automatic_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            original_states = {
                record["label"]: (
                    Path(record["path"]).read_bytes(),
                    Path(record["path"]).stat().st_mode & 0o7777,
                )
                if Path(record["path"]).exists()
                else None
                for record in fixture.records
            }
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            fixture.accept(journal_dir, "baseline")
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            audit_payload = {
                "status": "ok",
                "errors": [],
                "warnings": [],
                "audit": {
                    **manifest["audit"],
                    "scope": "live",
                    "stage": "builder",
                },
            }
            valid_runtime = [{"name": "db-mcp", "enabled": False}]
            invalid_builder = [{"name": "db-mcp", "enabled": True}]
            responses = iter(
                [audit_payload, valid_runtime, valid_runtime, invalid_builder]
            )

            def probe(_argv, _label, environment_overrides=None):
                payload = next(responses)
                return json.dumps(payload).encode("utf-8"), payload

            envelope = fixture.envelopes["builder"]
            output = io.StringIO()
            with mock.patch.object(
                host_migration_apply, "_run_json_command", side_effect=probe
            ), redirect_stdout(output):
                exit_code = host_migration_apply.main(
                    [
                        "--manifest",
                        str(fixture.manifest),
                        "--target-root",
                        str(fixture.target_root),
                        "--snapshot-receipt",
                        str(fixture.receipt),
                        "--approval-envelope",
                        str(envelope),
                        "--expected-envelope-sha256",
                        sha256(envelope),
                        "--expected-manifest-sha256",
                        sha256(fixture.manifest),
                        "--journal-dir",
                        str(journal_dir),
                        "--stage",
                        "builder",
                        "--confirm-apply",
                    ]
                )

            self.assertEqual(1, exit_code)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["recovery"]["attempted"])
            self.assertEqual("ok", payload["recovery"]["status"])
            self.assertEqual(
                "current config\n", fixture.first.read_text(encoding="utf-8")
            )
            self.assertFalse(fixture.profile.exists())
            restored_states = {
                record["label"]: (
                    Path(record["path"]).read_bytes(),
                    Path(record["path"]).stat().st_mode & 0o7777,
                )
                if Path(record["path"]).exists()
                else None
                for record in fixture.records
            }
            self.assertEqual(original_states, restored_states)
            journal = json.loads(
                (journal_dir / "journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual("rolled_back", journal["status"])

    def test_mid_apply_failure_records_partial_state_and_full_restore_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            original_install = host_migration_apply._install_target

            def fail_second(item, quarantine):
                if item.record["label"] == "codex-rules":
                    raise OSError("injected failure")
                original_install(item, quarantine)

            with mock.patch.object(
                host_migration_apply, "_install_target", side_effect=fail_second
            ):
                with self.assertRaises(ValueError):
                    fixture.apply(journal_dir, "baseline")

            self.assertEqual(TARGET_CONFIG, fixture.first.read_text(encoding="utf-8"))
            self.assertEqual("current rules\n", fixture.second.read_text(encoding="utf-8"))
            journal = json.loads((journal_dir / "journal.json").read_text(encoding="utf-8"))
            self.assertEqual("partial", journal["status"])
            self.assertTrue(journal["rollback_required"])
            self.assertEqual(["codex-config"], journal["completed"])
            self.assertEqual("codex-rules", journal["active_label"])
            self.assertEqual(str(fixture.receipt), journal["snapshot_receipt"])

    def test_failed_present_exchange_records_quarantine_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            original = host_migration_apply._verify_path_state

            def replace_installed_target(path, mode, digest, label):
                fixture.first.write_text("independent user file\n", encoding="utf-8")
                fixture.first.chmod(0o600)
                return original(path, mode, digest, label)

            with mock.patch.object(
                host_migration_apply,
                "_verify_path_state",
                side_effect=replace_installed_target,
            ):
                with self.assertRaises(ValueError):
                    fixture.apply(journal_dir, "baseline")

            journal = json.loads(
                (journal_dir / "journal.json").read_text(encoding="utf-8")
            )
            quarantine = Path(
                journal["retained_quarantines"]["apply:codex-config"]
            )
            self.assertEqual("partial", journal["status"])
            self.assertEqual("codex-config", journal["active_label"])
            self.assertEqual(
                "current config\n", quarantine.read_text(encoding="utf-8")
            )

    def test_interrupt_preserves_interrupt_semantics_and_partial_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            with mock.patch.object(
                host_migration_apply, "_install_target", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    fixture.apply(journal_dir, "baseline")
            journal = json.loads(
                (journal_dir / "journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual("partial", journal["status"])
            self.assertTrue(journal["rollback_required"])

    def test_confirmed_stages_must_follow_manifest_order_in_one_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"

            with self.assertRaises(ValueError):
                fixture.apply(journal_dir, "builder")
            self.assertFalse(journal_dir.exists())

            fixture.apply(journal_dir, "baseline")
            fixture.accept(journal_dir, "baseline")
            fixture.apply(journal_dir, "builder")
            journal = fixture.accept(journal_dir, "builder")

            self.assertEqual("ready", journal["status"])
            self.assertEqual(["baseline", "builder"], journal["completed_stages"])
            self.assertEqual(
                fixture.artifacts[fixture.profile].read_text(encoding="utf-8"),
                fixture.profile.read_text(encoding="utf-8"),
            )
            fixture.apply(journal_dir, "operator")
            journal = fixture.accept(journal_dir, "operator")
            self.assertEqual("complete", journal["status"])

    def test_next_stage_rejects_drift_in_a_completed_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            fixture.accept(journal_dir, "baseline")
            fixture.first.write_text("completed stage drift\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                fixture.apply(journal_dir, "builder")

            self.assertFalse(fixture.profile.exists())

    def test_rolled_back_journal_cannot_apply_a_later_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            rollback(journal_dir / "journal.json", True)

            self.assertEqual(
                "current config\n", fixture.first.read_text(encoding="utf-8")
            )
            with self.assertRaisesRegex(ValueError, "cannot be reused"):
                fixture.apply(journal_dir, "builder")

            self.assertFalse(fixture.profile.exists())

    def test_rollback_rejects_journal_that_omits_an_installed_target(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            journal_path = journal_dir / "journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal.update(
                {
                    "status": "partial",
                    "completed_stages": [],
                    "completed": [],
                    "active_stage": "baseline",
                    "active_label": None,
                    "active_attempt_id": "d" * 32,
                    "pending_audit_stage": None,
                    "pending_audit_envelope_sha256": None,
                    "rollback_required": True,
                    "stage_audits": {},
                }
            )
            journal_path.write_text(json.dumps(journal) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, "missing from journal progress"
            ):
                rollback(journal_path, True)

            self.assertEqual(
                TARGET_CONFIG, fixture.first.read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                "rolled_back",
                json.loads(journal_path.read_text(encoding="utf-8"))["status"],
            )

    def test_crash_state_requires_rollback_before_next_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            journal_dir = fixture.root / "journal"
            fixture.apply(journal_dir, "baseline")
            fixture.accept(journal_dir, "baseline")
            journal_path = journal_dir / "journal.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["status"] = "applying"
            journal["active_stage"] = "builder"
            journal["active_attempt_id"] = "d" * 32
            journal["stage_envelopes"]["builder"] = sha256(
                fixture.envelopes["builder"]
            )
            journal["stage_envelope_paths"]["builder"] = str(
                fixture.envelopes["builder"]
            )
            journal_path.write_text(json.dumps(journal) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be rolled back"):
                fixture.apply(journal_dir, "builder")

    def test_manifest_requires_ok_attestation_and_exact_stage_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            manifest["schema_version"] = 2
            fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema_version 3"):
                host_migration_apply.load_manifest(fixture.manifest)

        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            manifest["audit"]["status"] = "error"
            fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "attestation must have status ok"):
                host_migration_apply.load_manifest(fixture.manifest)

        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(
                directory,
                reviewer_target='[mcp_servers.db-mcp]\nenabled = false\n',
            )
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            credential = fixture.host / "dev" / "db-mcp" / "db-config.json"
            credential.parent.mkdir(parents=True)
            credential.write_text("{}\n", encoding="utf-8")
            record = next(
                item
                for item in manifest["targets"]
                if item["label"] == "codex-adapter-dba"
            )
            record["label"] = "db-config"
            record["path"] = str(credential)
            label_index = manifest["stages"][0]["labels"].index(
                "codex-adapter-dba"
            )
            manifest["stages"][0]["labels"][label_index] = "db-config"
            manifest["audit"]["target_inventory_sha256"] = (
                host_migration_apply._target_inventory_sha256(manifest)
            )
            fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside the host-policy allowlist"):
                host_migration_apply.load_manifest(fixture.manifest)

        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            manifest["audit"]["stage"] = "baseline"
            fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "requires a full target audit"):
                host_migration_apply.load_manifest(fixture.manifest)

        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            manifest["stages"][0]["labels"].remove("codex-rules")
            fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "assign every target exactly once"):
                host_migration_apply.load_manifest(fixture.manifest)

    def test_manifest_requires_every_profile_and_serena_project_target(self):
        missing_sets = (
            ("codex-profile-scout",),
            ("codex-profile-builder",),
            ("codex-profile-operator",),
            ("serena-project-project-one",),
            (
                "serena-project-project-one",
                "serena-project-project-two",
            ),
        )
        for missing in missing_sets:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                fixture = ApplyFixture(directory)
                manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
                manifest["targets"] = [
                    record
                    for record in manifest["targets"]
                    if record["label"] not in missing
                ]
                for stage in manifest["stages"]:
                    stage["labels"] = [
                        label for label in stage["labels"] if label not in missing
                    ]
                manifest["audit"]["target_inventory_sha256"] = (
                    host_migration_apply._target_inventory_sha256(manifest)
                )
                fixture.manifest.write_text(
                    json.dumps(manifest) + "\n", encoding="utf-8"
                )

                with self.assertRaisesRegex(
                    ValueError, "missing required|Serena project|non-empty"
                ):
                    host_migration_apply.load_manifest(fixture.manifest)

    def test_preflight_rejects_attested_codex_target_that_enables_db(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(directory)
            artifact = fixture.artifacts[fixture.first]
            artifact.write_text(
                "[mcp_servers.db-mcp]\nenabled = true\n", encoding="utf-8"
            )
            artifact.chmod(0o600)
            manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
            record = next(
                item for item in manifest["targets"] if item["label"] == "codex-config"
            )
            record["target_sha256"] = sha256(artifact)
            audit_input = next(
                item
                for item in manifest["audit"]["inputs"]
                if item["path"] == str(artifact)
            )
            audit_input["sha256"] = record["target_sha256"]
            canonical_inputs = json.dumps(
                manifest["audit"]["inputs"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            manifest["audit"]["inputs_sha256"] = hashlib.sha256(
                canonical_inputs
            ).hexdigest()
            manifest["audit"]["target_inventory_sha256"] = (
                host_migration_apply._target_inventory_sha256(manifest)
            )
            fixture.manifest.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "explicitly disabled"):
                host_migration_apply.preflight(
                    fixture.manifest,
                    fixture.target_root,
                    fixture.receipt,
                    "baseline",
                )

    def test_self_consistent_protected_reviewer_db_enable_is_rejected_prewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ApplyFixture(
                directory,
                reviewer_target='[mcp_servers.db-mcp]\nenabled = true\n',
            )

            with self.assertRaisesRegex(
                ValueError, "protected reviewer target must keep db-mcp"
            ):
                fixture.apply(fixture.root / "journal", "baseline")

            self.assertEqual(
                '[mcp_servers.db-mcp]\nenabled = false\n',
                fixture.reviewer.read_text(encoding="utf-8"),
            )
            self.assertEqual("current config\n", fixture.first.read_text(encoding="utf-8"))
            self.assertFalse((fixture.root / "journal").exists())


if __name__ == "__main__":
    unittest.main()

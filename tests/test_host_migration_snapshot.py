import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import host_migration_apply, host_migration_snapshot


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "host_migration_snapshot.py"
POLICY_SHA256 = host_migration_apply._canonical_policy_sha256(
    (ROOT / "policies" / "host-policy.json").read_bytes()
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def codex_file(root, name):
    if name == "default.rules":
        parent = root / "home" / ".codex" / "rules"
    else:
        parent = root / "home" / ".codex"
    parent.mkdir(parents=True, exist_ok=True)
    return parent / name


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *map(str, args)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def write_manifest(root, entries, stages=None):
    target_root = root / "target"
    target_root.mkdir(exist_ok=True)
    targets = []
    for label, path, state in entries:
        if state == "present":
            mode = "{:04o}".format(path.stat().st_mode & 0o7777)
            current_sha = sha256(path)
        else:
            mode = None
            current_sha = None
        targets.append(
            {
                "label": label,
                "path": str(path),
                "pre_state": state,
                "current_mode": mode,
                "current_sha256": current_sha,
                "target_mode": "0600",
                "target_sha256": "3" * 64,
            }
        )
    manifest = root / "manifest.json"
    stage_records = stages or [
        {"name": "baseline", "labels": [item["label"] for item in targets]}
    ]
    audit_inputs = [
        {
            "label": host_migration_apply._expected_target_receipt_label(
                record["label"]
            ).rstrip("*"),
            "path": str(
                target_root
                / Path(record["path"]).relative_to(Path(record["path"]).anchor)
            ),
            "mode": record["target_mode"],
            "sha256": record["target_sha256"],
        }
        for record in targets
    ]
    canonical_inputs = json.dumps(
        audit_inputs, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload = {
                "schema_version": 3,
                "audit": {
                    "status": "ok",
                    "scope": "target",
                    "stage": "full",
                    "validator": "validate_host_policy.py@3",
                    "policy_sha256": POLICY_SHA256,
                    "inputs_sha256": hashlib.sha256(canonical_inputs).hexdigest(),
                    "target_inventory_sha256": "0" * 64,
                    "target_root": str(target_root),
                    "inputs": audit_inputs,
                },
                "stages": stage_records,
                "targets": targets,
            }
    payload["audit"]["target_inventory_sha256"] = (
        host_migration_apply._target_inventory_sha256(payload)
    )
    manifest.write_text(
        json.dumps(payload)
        + "\n",
        encoding="utf-8",
    )
    return manifest


def set_target_state(manifest, label, content, mode=0o600):
    data = json.loads(manifest.read_text(encoding="utf-8"))
    record = next(item for item in data["targets"] if item["label"] == label)
    encoded = content.encode("utf-8") if isinstance(content, str) else content
    record["target_mode"] = "{:04o}".format(mode)
    record["target_sha256"] = hashlib.sha256(encoded).hexdigest()
    audit_input = next(
        item
        for item in data["audit"]["inputs"]
        if item["label"]
        == host_migration_apply._expected_target_receipt_label(label).rstrip("*")
    )
    audit_input["mode"] = record["target_mode"]
    audit_input["sha256"] = record["target_sha256"]
    canonical_inputs = json.dumps(
        data["audit"]["inputs"], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    data["audit"]["inputs_sha256"] = hashlib.sha256(canonical_inputs).hexdigest()
    data["audit"]["target_inventory_sha256"] = (
        host_migration_apply._target_inventory_sha256(data)
    )
    manifest.write_text(json.dumps(data) + "\n", encoding="utf-8")


def write_journal(
    root,
    manifest,
    receipt,
    completed_stages=None,
    completed=None,
    status=None,
    active_stage=None,
    active_label=None,
    rollback_required=False,
):
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    stage_names = [item["name"] for item in manifest_data["stages"]]
    if completed_stages is None:
        completed_stages = stage_names
    if completed is None:
        completed = [
            label
            for stage in manifest_data["stages"]
            if stage["name"] in completed_stages
            for label in stage["labels"]
        ]
    if status is None:
        status = "complete" if completed_stages == stage_names else "ready"
    approved_stages = list(completed_stages)
    if active_stage is not None and active_stage not in approved_stages:
        approved_stages.append(active_stage)
    bound_envelope = root / "bound-approval-envelope.json"
    bound_envelope.write_text(
        json.dumps(
            {
                "executors": [
                    {
                        "relative_path": "scripts/{}".format(filename),
                        "path": str(ROOT / "scripts" / filename),
                        "sha256": sha256(ROOT / "scripts" / filename),
                    }
                    for filename in (
                        "host_migration_apply.py",
                        "host_migration_envelope.py",
                        "host_migration_snapshot.py",
                    )
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    stage_envelopes = {name: sha256(bound_envelope) for name in approved_stages}
    stage_envelope_paths = {
        name: str(bound_envelope) for name in approved_stages
    }
    stage_audits = {
        name: {
            "audit_file": "{}-live-audit.json".format(name),
            "audit_sha256": "a" * 64,
            "runtime_file": "{}-runtime-mcp.json".format(name),
            "runtime_sha256": "b" * 64,
            "live_state_sha256": "c" * 64,
        }
        for name in completed_stages
    }
    journal_dir = root / "journal"
    journal_dir.mkdir(mode=0o700)
    journal = journal_dir / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "manifest": str(manifest),
                "manifest_sha256": sha256(manifest),
                "snapshot_receipt": str(receipt),
                "snapshot_receipt_sha256": sha256(receipt),
                "target_root": str(root / "target"),
                "stages": stage_names,
                "status": status,
                "completed_stages": completed_stages,
                "completed": completed,
                "active_stage": active_stage,
                "active_label": active_label,
                "active_attempt_id": (
                    "d" * 32
                    if status in {"applying", "partial", "awaiting_stage_audit"}
                    else None
                ),
                "rollback_required": rollback_required,
                "retained_quarantines": {},
                "stage_envelopes": stage_envelopes,
                "stage_envelope_paths": stage_envelope_paths,
                "stage_audits": stage_audits,
                "pending_audit_stage": None,
                "pending_audit_envelope_sha256": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    journal.chmod(0o600)
    return journal


class HostMigrationSnapshotTests(unittest.TestCase):
    def test_restore_persists_mode_before_file_fsync_and_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve() / "restored"
            events = []
            original_fchmod = os.fchmod
            original_fsync = os.fsync

            def record_fchmod(descriptor, mode):
                events.append(("fchmod", mode))
                return original_fchmod(descriptor, mode)

            def record_fsync(descriptor):
                events.append(("fsync", descriptor))
                return original_fsync(descriptor)

            with mock.patch.object(
                host_migration_snapshot.os, "fchmod", side_effect=record_fchmod
            ), mock.patch.object(
                host_migration_snapshot.os, "fsync", side_effect=record_fsync
            ):
                host_migration_snapshot._restore_backup_noreplace(
                    source, b"restored\n", "0640"
                )

            self.assertLess(
                next(index for index, event in enumerate(events) if event[0] == "fchmod"),
                next(index for index, event in enumerate(events) if event[0] == "fsync"),
            )
            self.assertEqual(0o640, source.stat().st_mode & 0o777)

    def test_cli_argument_errors_are_structured_json(self):
        result = run_cli("--definitely-invalid")

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("error", payload["status"])
        self.assertIn("argument error", payload["issues"][0]["message"])

    def test_target_digest_matches_manifest_attestation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = codex_file(root, "config.toml")
            source.write_text("approved\n", encoding="utf-8")
            manifest = write_manifest(root, (("codex-config", source, "present"),))

            result = run_cli("target-digest", "--manifest", manifest)

            self.assertEqual(0, result.returncode, result.stdout)
            payload = json.loads(result.stdout)
            expected = json.loads(manifest.read_text(encoding="utf-8"))["audit"][
                "target_inventory_sha256"
            ]
            self.assertEqual(expected, payload["target_inventory_sha256"])

    def test_prepare_publish_never_replaces_concurrent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = codex_file(root, "config.toml")
            source.write_text("approved\n", encoding="utf-8")
            manifest = write_manifest(root, (("codex-config", source, "present"),))
            output = root / "snapshot"
            original = host_migration_snapshot._rename_noreplace
            competing_inode = None

            def create_competing_directory(staging, target):
                nonlocal competing_inode
                target.mkdir()
                competing_inode = target.stat().st_ino
                return original(staging, target)

            with mock.patch.object(
                host_migration_snapshot,
                "_rename_noreplace",
                side_effect=create_competing_directory,
            ):
                with self.assertRaisesRegex(ValueError, "appeared during publication"):
                    host_migration_snapshot.prepare(manifest, output)

            self.assertTrue(output.is_dir())
            self.assertEqual(competing_inode, output.stat().st_ino)
            self.assertEqual([], list(output.iterdir()))

    def test_prepare_rechecks_absent_state_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = codex_file(root, "scout.config.toml")
            manifest = write_manifest(
                root, (("codex-profile-scout", profile, "absent"),)
            )
            output = root / "snapshot"
            original = host_migration_apply.bind_snapshot

            def create_profile_after_snapshot(*args, **kwargs):
                result = original(*args, **kwargs)
                profile.write_text("independent user file\n", encoding="utf-8")
                return result

            with mock.patch.object(
                host_migration_apply,
                "bind_snapshot",
                side_effect=create_profile_after_snapshot,
            ):
                with self.assertRaisesRegex(ValueError, "state differs"):
                    host_migration_snapshot.prepare(manifest, output)

            self.assertFalse(output.exists())
            self.assertEqual(
                "independent user file\n", profile.read_text(encoding="utf-8")
            )

    def test_prepare_derives_complete_inventory_and_private_modes_from_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = codex_file(root, "config.toml")
            second = codex_file(root, "default.rules")
            first.write_text("safe config\n", encoding="utf-8")
            second.write_text("safe rules\n", encoding="utf-8")
            first.chmod(0o600)
            second.chmod(0o640)
            manifest = write_manifest(
                root,
                (
                    ("codex-config", first, "present"),
                    ("codex-rules", second, "present"),
                ),
            )
            output = root / "snapshot"

            result = run_cli(
                "prepare", "--manifest", manifest, "--output-dir", output
            )

            self.assertEqual(0, result.returncode, result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual("ok", payload["status"])
            self.assertEqual(sha256(manifest), payload["manifest_sha256"])
            self.assertEqual(0o700, output.stat().st_mode & 0o777)
            self.assertEqual(0o600, (output / "receipt.json").stat().st_mode & 0o777)
            records = {item["label"]: item for item in payload["receipt"]["files"]}
            self.assertEqual("0600", records["codex-config"]["mode"])
            self.assertEqual("0640", records["codex-rules"]["mode"])

    def test_prepare_rejects_manifest_prestate_drift_before_creating_output(self):
        mutations = (
            ("bytes", lambda path: path.write_text("changed\n", encoding="utf-8")),
            ("mode", lambda path: path.chmod(0o644)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source = codex_file(root, "config.toml")
                source.write_text("approved\n", encoding="utf-8")
                source.chmod(0o600)
                manifest = write_manifest(
                    root, (("codex-config", source, "present"),)
                )
                mutate(source)
                output = root / "snapshot"

                result = run_cli(
                    "prepare", "--manifest", manifest, "--output-dir", output
                )

                self.assertEqual(1, result.returncode)
                self.assertIn("manifest source state differs", result.stdout)
                self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = codex_file(root, "scout.config.toml")
            manifest = write_manifest(
                root, (("codex-profile-scout", profile, "absent"),)
            )
            profile.write_text("unexpected\n", encoding="utf-8")
            output = root / "snapshot"

            result = run_cli(
                "prepare", "--manifest", manifest, "--output-dir", output
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("manifest source state differs", result.stdout)
            self.assertFalse(output.exists())

    def test_prepare_does_not_publish_snapshot_when_manifest_changes_during_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = codex_file(root, "config.toml")
            source.write_text("approved\n", encoding="utf-8")
            manifest = write_manifest(root, (("codex-config", source, "present"),))
            output = root / "snapshot"
            original_snapshot = host_migration_snapshot.snapshot

            def mutate_manifest_after_copy(*args, **kwargs):
                receipt = original_snapshot(*args, **kwargs)
                data = json.loads(manifest.read_text(encoding="utf-8"))
                data["audit"]["inputs_sha256"] = "9" * 64
                manifest.write_text(json.dumps(data) + "\n", encoding="utf-8")
                return receipt

            with mock.patch.object(
                host_migration_snapshot,
                "snapshot",
                side_effect=mutate_manifest_after_copy,
            ):
                with self.assertRaisesRegex(ValueError, "manifest changed"):
                    host_migration_snapshot.prepare(manifest, output)

            self.assertFalse(output.exists())
            self.assertEqual(
                [], list(root.glob(".host-policy-snapshot.*"))
            )

    def test_verify_detects_drift_and_journal_rollback_recovers_bytes_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = codex_file(root, "config.toml")
            source.write_text("before\n", encoding="utf-8")
            source.chmod(0o600)
            manifest = write_manifest(root, (("codex-config", source, "present"),))
            set_target_state(manifest, "codex-config", "after\n", 0o644)
            output = root / "snapshot"
            created = run_cli(
                "prepare", "--manifest", manifest, "--output-dir", output
            )
            self.assertEqual(0, created.returncode, created.stdout)
            receipt = output / "receipt.json"
            journal = write_journal(root, manifest, receipt)
            source.write_text("after\n", encoding="utf-8")
            source.chmod(0o644)

            verify_source = run_cli("verify", "--receipt", receipt, "--scope", "source")
            denied = run_cli("rollback", "--journal", journal)
            restored = run_cli(
                "rollback", "--journal", journal, "--confirm-rollback"
            )

            self.assertEqual(1, verify_source.returncode)
            self.assertEqual(1, denied.returncode)
            self.assertEqual(0, restored.returncode, restored.stdout)
            self.assertEqual("before\n", source.read_text(encoding="utf-8"))
            self.assertEqual(0o600, source.stat().st_mode & 0o777)
            terminal = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual("rolled_back", terminal["status"])
            self.assertEqual([], terminal["completed"])
            self.assertEqual(["codex-config"], terminal["rolled_back_labels"])
            repeated = run_cli(
                "rollback", "--journal", journal, "--confirm-rollback"
            )
            self.assertEqual(1, repeated.returncode)
            self.assertIn("already been rolled back", repeated.stdout)

    def test_prepare_rejects_symlink_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            actual = codex_file(root, "actual.toml")
            actual.write_text("config\n", encoding="utf-8")
            link = codex_file(root, "config.toml")
            link.symlink_to(actual)
            manifest = write_manifest(root, (("codex-config", actual, "present"),))
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["targets"][0]["path"] = str(link)
            data["audit"]["inputs"][0]["path"] = str(
                root / "target" / link.relative_to(link.anchor)
            )
            canonical_inputs = json.dumps(
                data["audit"]["inputs"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            data["audit"]["inputs_sha256"] = hashlib.sha256(
                canonical_inputs
            ).hexdigest()
            data["audit"]["target_inventory_sha256"] = (
                host_migration_apply._target_inventory_sha256(data)
            )
            manifest.write_text(json.dumps(data), encoding="utf-8")

            result = run_cli(
                "prepare", "--manifest", manifest, "--output-dir", root / "snapshot"
            )

        self.assertEqual(1, result.returncode)
        self.assertIn("regular non-symlink", result.stdout)

    def test_rollback_quarantines_target_that_was_absent_at_prepare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = codex_file(root, "scout.config.toml")
            manifest = write_manifest(
                root, (("codex-profile-scout", profile, "absent"),)
            )
            set_target_state(
                manifest,
                "codex-profile-scout",
                'sandbox_mode = "read-only"\n',
            )
            output = root / "snapshot"
            created = run_cli(
                "prepare", "--manifest", manifest, "--output-dir", output
            )
            self.assertEqual(0, created.returncode, created.stdout)
            receipt = output / "receipt.json"
            journal = write_journal(root, manifest, receipt)
            profile.write_text('sandbox_mode = "read-only"\n', encoding="utf-8")
            profile.chmod(0o600)

            restored = run_cli(
                "rollback", "--journal", journal, "--confirm-rollback"
            )

            self.assertEqual(0, restored.returncode, restored.stdout)
            payload = json.loads(restored.stdout)
            self.assertEqual(
                ["codex-profile-scout"],
                payload["removed_new_files"],
            )
            self.assertFalse(profile.exists())
            quarantine = Path(
                payload["retained_quarantines"]["rollback:codex-profile-scout"]
            )
            self.assertEqual(
                'sandbox_mode = "read-only"\n',
                quarantine.read_text(encoding="utf-8"),
            )

    def test_rollback_never_deletes_a_replaced_quarantine_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = codex_file(root, "scout.config.toml")
            manifest = write_manifest(
                root, (("codex-profile-scout", profile, "absent"),)
            )
            set_target_state(manifest, "codex-profile-scout", "approved profile\n")
            output = root / "snapshot"
            host_migration_snapshot.prepare(manifest, output)
            journal = write_journal(root, manifest, output / "receipt.json")
            profile.write_text("approved profile\n", encoding="utf-8")
            profile.chmod(0o600)
            original = host_migration_snapshot._path_matches
            replaced = False

            def replace_after_verification(path, mode, digest, label):
                nonlocal replaced
                result = original(path, mode, digest, label)
                if result and label == "quarantined target" and not replaced:
                    replaced = True
                    competitor = path.parent / ".independent-quarantine-file"
                    competitor.write_text("independent user file\n", encoding="utf-8")
                    competitor.chmod(0o600)
                    os.replace(competitor, path)
                return result

            with mock.patch.object(
                host_migration_snapshot,
                "_path_matches",
                side_effect=replace_after_verification,
            ):
                result = host_migration_snapshot.rollback(journal, True)

            quarantine = Path(
                result["retained_quarantines"]["rollback:codex-profile-scout"]
            )
            self.assertFalse(profile.exists())
            self.assertEqual(
                "independent user file\n", quarantine.read_text(encoding="utf-8")
            )

    def test_rollback_leaves_unapplied_stage_labels_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = codex_file(root, "config.toml")
            profile = codex_file(root, "builder.config.toml")
            source.write_text("before\n", encoding="utf-8")
            source.chmod(0o600)
            stages = [
                {"name": "baseline", "labels": ["codex-config"]},
                {"name": "builder", "labels": ["codex-profile-builder"]},
            ]
            manifest = write_manifest(
                root,
                (
                    ("codex-config", source, "present"),
                    ("codex-profile-builder", profile, "absent"),
                ),
                stages=stages,
            )
            set_target_state(manifest, "codex-config", "applied\n")
            output = root / "snapshot"
            created = run_cli(
                "prepare", "--manifest", manifest, "--output-dir", output
            )
            self.assertEqual(0, created.returncode, created.stdout)
            journal = write_journal(
                root,
                manifest,
                output / "receipt.json",
                completed_stages=["baseline"],
                completed=["codex-config"],
                status="ready",
            )
            source.write_text("applied\n", encoding="utf-8")
            profile.write_text("independent user file\n", encoding="utf-8")

            restored = run_cli(
                "rollback", "--journal", journal, "--confirm-rollback"
            )

            self.assertEqual(0, restored.returncode, restored.stdout)
            self.assertEqual("before\n", source.read_text(encoding="utf-8"))
            self.assertEqual(
                "independent user file\n", profile.read_text(encoding="utf-8")
            )
            self.assertEqual([], json.loads(restored.stdout)["removed_new_files"])

    def test_rollback_includes_the_active_uncertain_label_after_a_partial_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = codex_file(root, "config.toml")
            profile = codex_file(root, "builder.config.toml")
            source.write_text("before\n", encoding="utf-8")
            source.chmod(0o600)
            stages = [
                {"name": "baseline", "labels": ["codex-config"]},
                {"name": "builder", "labels": ["codex-profile-builder"]},
            ]
            manifest = write_manifest(
                root,
                (
                    ("codex-config", source, "present"),
                    ("codex-profile-builder", profile, "absent"),
                ),
                stages=stages,
            )
            set_target_state(manifest, "codex-config", "baseline applied\n")
            set_target_state(
                manifest,
                "codex-profile-builder",
                "uncertain partial write\n",
            )
            output = root / "snapshot"
            created = run_cli(
                "prepare", "--manifest", manifest, "--output-dir", output
            )
            self.assertEqual(0, created.returncode, created.stdout)
            journal = write_journal(
                root,
                manifest,
                output / "receipt.json",
                completed_stages=["baseline"],
                completed=["codex-config"],
                status="partial",
                active_stage="builder",
                active_label="codex-profile-builder",
                rollback_required=True,
            )
            source.write_text("baseline applied\n", encoding="utf-8")
            source.chmod(0o600)
            profile.write_text("uncertain partial write\n", encoding="utf-8")
            profile.chmod(0o600)

            restored = run_cli(
                "rollback", "--journal", journal, "--confirm-rollback"
            )

            self.assertEqual(0, restored.returncode, restored.stdout)
            payload = json.loads(restored.stdout)
            self.assertEqual("before\n", source.read_text(encoding="utf-8"))
            self.assertFalse(profile.exists())
            self.assertEqual(
                ["codex-profile-builder"], payload["removed_new_files"]
            )

    def test_applying_crash_rolls_back_completed_and_active_uncertain_scope(self):
        for active_installed in (False, True):
            with self.subTest(active_installed=active_installed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source = codex_file(root, "config.toml")
                profile = codex_file(root, "builder.config.toml")
                source.write_text("before\n", encoding="utf-8")
                source.chmod(0o600)
                stages = [
                    {"name": "baseline", "labels": ["codex-config"]},
                    {"name": "builder", "labels": ["codex-profile-builder"]},
                ]
                manifest = write_manifest(
                    root,
                    (
                        ("codex-config", source, "present"),
                        ("codex-profile-builder", profile, "absent"),
                    ),
                    stages=stages,
                )
                set_target_state(manifest, "codex-config", "baseline applied\n")
                set_target_state(
                    manifest, "codex-profile-builder", "builder target\n"
                )
                output = root / "snapshot"
                host_migration_snapshot.prepare(manifest, output)
                journal = write_journal(
                    root,
                    manifest,
                    output / "receipt.json",
                    completed_stages=["baseline"],
                    completed=["codex-config"],
                    status="applying",
                    active_stage="builder",
                    active_label=(
                        "codex-profile-builder" if active_installed else None
                    ),
                )
                source.write_text("baseline applied\n", encoding="utf-8")
                source.chmod(0o600)
                if active_installed:
                    profile.write_text("builder target\n", encoding="utf-8")
                    profile.chmod(0o600)

                result = run_cli(
                    "rollback", "--journal", journal, "--confirm-rollback"
                )

                self.assertEqual(0, result.returncode, result.stdout)
                self.assertEqual("before\n", source.read_text(encoding="utf-8"))
                self.assertFalse(profile.exists())
                terminal = json.loads(journal.read_text(encoding="utf-8"))
                self.assertEqual("rolled_back", terminal["status"])
                expected = ["codex-config"]
                if active_installed:
                    expected.append("codex-profile-builder")
                self.assertEqual(expected, terminal["rolled_back_labels"])

    def test_rollback_rejects_drifted_absent_target_before_any_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = codex_file(root, "config.toml")
            profile = codex_file(root, "scout.config.toml")
            source.write_text("before\n", encoding="utf-8")
            manifest = write_manifest(
                root,
                (
                    ("codex-config", source, "present"),
                    ("codex-profile-scout", profile, "absent"),
                ),
            )
            set_target_state(manifest, "codex-config", "applied\n")
            set_target_state(manifest, "codex-profile-scout", "approved profile\n")
            output = root / "snapshot"
            self.assertEqual(
                0,
                run_cli(
                    "prepare", "--manifest", manifest, "--output-dir", output
                ).returncode,
            )
            journal = write_journal(root, manifest, output / "receipt.json")
            source.write_text("applied\n", encoding="utf-8")
            profile.write_text("independent user content\n", encoding="utf-8")

            result = run_cli(
                "rollback", "--journal", journal, "--confirm-rollback"
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("differs from both approved states", result.stdout)
            self.assertEqual("applied\n", source.read_text(encoding="utf-8"))
            self.assertEqual(
                "independent user content\n", profile.read_text(encoding="utf-8")
            )

    def test_rollback_isolation_race_restores_independent_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = codex_file(root, "scout.config.toml")
            manifest = write_manifest(
                root, (("codex-profile-scout", profile, "absent"),)
            )
            set_target_state(manifest, "codex-profile-scout", "approved profile\n")
            output = root / "snapshot"
            host_migration_snapshot.prepare(manifest, output)
            journal = write_journal(root, manifest, output / "receipt.json")
            profile.write_text("approved profile\n", encoding="utf-8")
            profile.chmod(0o600)
            original = host_migration_snapshot._rename_noreplace
            injected = False

            def replace_before_isolation(source, target):
                nonlocal injected
                if not injected and source == profile:
                    injected = True
                    source.write_text("independent user file\n", encoding="utf-8")
                    source.chmod(0o600)
                return original(source, target)

            with mock.patch.object(
                host_migration_snapshot,
                "_rename_noreplace",
                side_effect=replace_before_isolation,
            ):
                with self.assertRaisesRegex(ValueError, "changed during rollback isolation"):
                    host_migration_snapshot.rollback(journal, True)

            self.assertEqual(
                "independent user file\n", profile.read_text(encoding="utf-8")
            )

    def test_rollback_terminal_write_failure_is_retry_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = codex_file(root, "scout.config.toml")
            manifest = write_manifest(
                root, (("codex-profile-scout", profile, "absent"),)
            )
            set_target_state(manifest, "codex-profile-scout", "approved profile\n")
            output = root / "snapshot"
            host_migration_snapshot.prepare(manifest, output)
            journal = write_journal(root, manifest, output / "receipt.json")
            profile.write_text("approved profile\n", encoding="utf-8")
            profile.chmod(0o600)
            original_write = host_migration_snapshot._write_journal

            def fail_terminal_write(path, payload):
                if payload["status"] == "rolled_back":
                    raise OSError("injected terminal write failure")
                original_write(path, payload)

            with mock.patch.object(
                host_migration_snapshot,
                "_write_journal",
                side_effect=fail_terminal_write,
            ):
                with self.assertRaisesRegex(OSError, "terminal write failure"):
                    host_migration_snapshot.rollback(journal, True)

            durable = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual("rolling_back", durable["status"])
            self.assertEqual(
                ["codex-profile-scout"], durable["rolled_back_labels"]
            )
            profile.write_text("new independent file\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "both approved states"):
                host_migration_snapshot.rollback(journal, True)

            self.assertEqual(
                "new independent file\n", profile.read_text(encoding="utf-8")
            )

    def test_rollback_preflights_every_selected_target_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = codex_file(root, "config.toml")
            second = codex_file(root, "default.rules")
            first.write_text("first before\n", encoding="utf-8")
            second.write_text("second before\n", encoding="utf-8")
            manifest = write_manifest(
                root,
                (
                    ("codex-config", first, "present"),
                    ("codex-rules", second, "present"),
                ),
            )
            set_target_state(manifest, "codex-config", "first applied\n")
            output = root / "snapshot"
            created = run_cli(
                "prepare", "--manifest", manifest, "--output-dir", output
            )
            self.assertEqual(0, created.returncode, created.stdout)
            journal = write_journal(root, manifest, output / "receipt.json")
            first.write_text("first applied\n", encoding="utf-8")
            first.chmod(0o600)
            second.unlink()
            second.symlink_to(first)

            restored = run_cli(
                "rollback", "--journal", journal, "--confirm-rollback"
            )

            self.assertEqual(1, restored.returncode)
            self.assertIn("differs from both approved states", restored.stdout)
            self.assertEqual("first applied\n", first.read_text(encoding="utf-8"))

    def test_rollback_rejects_backup_swap_before_installing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = codex_file(root, "config.toml")
            source.write_text("before\n", encoding="utf-8")
            manifest = write_manifest(root, (("codex-config", source, "present"),))
            set_target_state(manifest, "codex-config", "applied\n")
            output = root / "snapshot"
            host_migration_snapshot.prepare(manifest, output)
            receipt = output / "receipt.json"
            journal = write_journal(root, manifest, receipt)
            source.write_text("applied\n", encoding="utf-8")
            source.chmod(0o600)
            backup = output / "files" / "codex-config"
            preserved = output / "files" / "preserved"
            evil = root / "evil"
            evil.write_text("evil\n", encoding="utf-8")
            original_scope = host_migration_snapshot._validate_live_rollback_scope

            def swap_backup(*args, **kwargs):
                states = original_scope(*args, **kwargs)
                backup.rename(preserved)
                backup.symlink_to(evil)
                return states

            with mock.patch.object(
                host_migration_snapshot,
                "_validate_live_rollback_scope",
                side_effect=swap_backup,
            ):
                with self.assertRaisesRegex(ValueError, "regular non-symlink"):
                    host_migration_snapshot.rollback(journal, True)

            self.assertEqual("applied\n", source.read_text(encoding="utf-8"))

    def test_rollback_rejects_receipt_path_redirect_even_when_journal_digest_is_updated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = codex_file(root, "config.toml")
            source.write_text("before\n", encoding="utf-8")
            manifest = write_manifest(root, (("codex-config", source, "present"),))
            output = root / "snapshot"
            created = run_cli(
                "prepare", "--manifest", manifest, "--output-dir", output
            )
            self.assertEqual(0, created.returncode, created.stdout)
            receipt = output / "receipt.json"
            receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
            receipt_data["files"][0]["source"] = str(root / "other.toml")
            receipt.write_text(json.dumps(receipt_data), encoding="utf-8")
            journal = write_journal(root, manifest, receipt)

            result = run_cli(
                "rollback", "--journal", journal, "--confirm-rollback"
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("identity differs from manifest", result.stdout)


if __name__ == "__main__":
    unittest.main()

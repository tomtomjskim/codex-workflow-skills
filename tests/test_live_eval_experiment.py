import ast
import hashlib
import io
import inspect
import itertools
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields, replace
from pathlib import Path
from types import MappingProxyType
from unittest import mock

import scripts.run_harness_experiment as experiment_cli
import scripts.live_eval.experiment as experiment_module
from scripts.live_eval.checkout import CheckoutManifest
from scripts.live_eval.experiment import (
    ExperimentPreflightRequest,
    ExperimentPreflightError,
    ExperimentPreflightResult,
    _allowed_write_policy_digest,
    _base_profile_digest,
    _begin_handoff,
    _classify_allowed_write_paths,
    _cleanup_owned_phase,
    _complete_handoff,
    _create_owned_directory,
    _create_phase_ledger,
    _open_physical_directory_gate,
    _require_base_profile_pair,
    _require_physical_disjointness,
    _resolve_empty_handoff,
    run_experiment_preflight,
)
from scripts.live_eval.experiment_plan import load_experiment_input
from scripts.live_eval.harness import HarnessManifest
from scripts.live_eval.task_snapshot import TaskSourceSpec, TaskTreeEntry
from scripts.workflow_coordination.canonical_json import canonical_bytes


def _digest(label):
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _oid(label, length=40):
    value = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return (value * 2)[:length]


class _Abort(BaseException):
    pass


class _AlwaysEqualValue:
    def __init__(self, payload=None):
        self.payload = payload

    def __eq__(self, unused_other):
        return True

    def __ne__(self, unused_other):
        return False


class ExperimentResultContractTests(unittest.TestCase):
    _DIGEST_FIELDS = (
        "bundle_digest",
        "current_profile_digest",
        "lean_profile_digest",
        "task_corpus_receipt_digest",
        "plan_digest",
        "preflight_receipt_digest",
    )
    _VALID_MASKS = {
        "000000",
        "100000",
        "110000",
        "111000",
        "111100",
        "111110",
        "111111",
    }

    def _result(self, mask):
        digests = {
            name: (_digest(name) if bit == "1" else None)
            for name, bit in zip(self._DIGEST_FIELDS, mask)
        }
        success = mask == "111111"
        cleanup_required = mask == "111110"
        qualified = mask in {"111100", "111110", "111111"}
        return ExperimentPreflightResult(
            status="static_only" if success else "blocked",
            live_backend_state="live_backend_not_implemented",
            global_agents_marker_state="global_agents_marker_not_run",
            pilot_state="pilot_not_run",
            qualification_evidence_classification=(
                "operator_attested_static" if qualified else "not_validated"
            ),
            model_calls=0,
            materialization_result="verified" if success else "blocked",
            plan_digest=digests["plan_digest"],
            task_corpus_receipt_digest=digests[
                "task_corpus_receipt_digest"
            ],
            preflight_receipt_digest=digests["preflight_receipt_digest"],
            bundle_digest=digests["bundle_digest"],
            current_profile_digest=digests["current_profile_digest"],
            lean_profile_digest=digests["lean_profile_digest"],
            cleanup_state=(
                "cleanup_required" if cleanup_required else "removed"
            ),
            reason_code=(
                "static_preflight_verified"
                if success
                else (
                    "task_snapshot_cleanup_required"
                    if cleanup_required
                    else "experiment_preflight_invalid"
                )
            ),
        )

    def test_result_has_exact_public_field_order(self):
        self.assertEqual(
            tuple(item.name for item in fields(ExperimentPreflightResult)),
            (
                "status",
                "live_backend_state",
                "global_agents_marker_state",
                "pilot_state",
                "qualification_evidence_classification",
                "model_calls",
                "materialization_result",
                "plan_digest",
                "task_corpus_receipt_digest",
                "preflight_receipt_digest",
                "bundle_digest",
                "current_profile_digest",
                "lean_profile_digest",
                "cleanup_state",
                "reason_code",
            ),
        )

    def test_only_seven_prefix_presence_masks_are_valid(self):
        for bits in itertools.product("01", repeat=6):
            mask = "".join(bits)
            with self.subTest(mask=mask):
                if mask in self._VALID_MASKS:
                    self.assertIsInstance(
                        self._result(mask), ExperimentPreflightResult
                    )
                else:
                    with self.assertRaisesRegex(
                        ExperimentPreflightError,
                        "^experiment_preflight_invalid$",
                    ):
                        self._result(mask)

    def test_cleanup_required_is_blocked_without_preflight_receipt(self):
        result = self._result("111110")
        result = ExperimentPreflightResult(
            **{
                **vars(result),
                "cleanup_state": "cleanup_required",
                "reason_code": "task_snapshot_cleanup_required",
            }
        )
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.preflight_receipt_digest)

    def test_nonempty_digest_prefix_cannot_be_not_started(self):
        for mask in sorted(self._VALID_MASKS - {"000000"}):
            with self.subTest(mask=mask):
                result = self._result(mask)
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^experiment_preflight_invalid$",
                ):
                    ExperimentPreflightResult(
                        **{
                            **vars(result),
                            "cleanup_state": "not_started",
                        }
                    )

    def test_success_state_is_exact(self):
        result = self._result("111111")
        self.assertEqual(result.status, "static_only")
        self.assertEqual(result.materialization_result, "verified")
        self.assertEqual(result.cleanup_state, "removed")
        self.assertEqual(result.reason_code, "static_preflight_verified")
        self.assertEqual(result.model_calls, 0)


class BaseProfileContractTests(unittest.TestCase):
    def _manifest(self, profile, agents_hash, home_digest):
        checkout = CheckoutManifest(
            object_format="sha1",
            tree_hash="sha1:" + _oid("tree"),
            plugin_blob_oid="sha1:" + _oid("plugin"),
            plugin_manifest_hash=_digest("plugin-manifest"),
            skill_hashes={
                "adversarial-review-loop": _digest("skill-source")
            },
            materialized_hashes={
                "adversarial-review-loop": _digest("skill-materialized")
            },
            skill_names=("adversarial-review-loop",),
        )
        return HarnessManifest(
            bundle_id="fixture-v1",
            bundle_digest=_digest("bundle"),
            profile=profile,
            checkout=checkout,
            agents_hash=agents_hash,
            skill_routing_hash=_digest("skill-routing"),
            adapter_source_hash=_digest("adapter-source"),
            adapter_materialized_hash=_digest("adapter-materialized"),
            common_role_hash=_digest("common-role"),
            home_digest=home_digest,
            adapter_count=16,
            role_count=16,
        )

    def test_base_profile_uses_exact_literal_path_free_projection(self):
        manifest = self._manifest(
            "current", _digest("current-agents"), _digest("current-home")
        )
        expected = {
            "adapter_count": 16,
            "adapter_materialized_hash": _digest("adapter-materialized"),
            "adapter_source_hash": _digest("adapter-source"),
            "agents_hash": _digest("current-agents"),
            "bundle_digest": _digest("bundle"),
            "bundle_id": "fixture-v1",
            "checkout": {
                "materialized_hashes": {
                    "adversarial-review-loop": _digest(
                        "skill-materialized"
                    )
                },
                "object_format": "sha1",
                "plugin_blob_oid": "sha1:" + _oid("plugin"),
                "plugin_manifest_hash": _digest("plugin-manifest"),
                "skill_hashes": {
                    "adversarial-review-loop": _digest("skill-source")
                },
                "skill_names": ["adversarial-review-loop"],
                "tree_hash": "sha1:" + _oid("tree"),
            },
            "common_role_hash": _digest("common-role"),
            "document_type": "harness-experiment-base-profile-v1",
            "home_digest": _digest("current-home"),
            "profile": "current",
            "role_count": 16,
            "schema_version": 1,
            "skill_routing_hash": _digest("skill-routing"),
        }
        expected_digest = (
            "sha256:" + hashlib.sha256(canonical_bytes(expected)).hexdigest()
        )
        self.assertEqual(_base_profile_digest(manifest), expected_digest)
        self.assertNotIn(
            str(Path(tempfile.gettempdir())),
            canonical_bytes(expected).decode("utf-8"),
        )

    def test_current_and_lean_profile_digests_are_distinct(self):
        current = self._manifest(
            "current", _digest("current-agents"), _digest("current-home")
        )
        lean = self._manifest(
            "lean", _digest("lean-agents"), _digest("lean-home")
        )
        self.assertNotEqual(
            _base_profile_digest(current), _base_profile_digest(lean)
        )

    def test_pair_requires_exact_shared_identity_and_distinct_profiles(self):
        current = self._manifest(
            "current", _digest("current-agents"), _digest("current-home")
        )
        lean = self._manifest(
            "lean", _digest("lean-agents"), _digest("lean-home")
        )
        self.assertEqual(
            _require_base_profile_pair(current, lean),
            (
                _base_profile_digest(current),
                _base_profile_digest(lean),
            ),
        )
        invalid_pairs = (
            (current, replace(lean, bundle_digest=_digest("other-bundle"))),
            (current, replace(lean, profile="current")),
            (current, replace(lean, agents_hash=current.agents_hash)),
            (current, replace(lean, home_digest=current.home_digest)),
        )
        for pair in invalid_pairs:
            with self.subTest(pair=pair):
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^harness_preflight_blocked$",
                ):
                    _require_base_profile_pair(*pair)


class AllowedWritePolicyTests(unittest.TestCase):
    def setUp(self):
        self.entries = (
            TaskTreeEntry(
                path="src/existing.py",
                git_mode="100644",
                blob_oid=_oid("existing"),
                size=1,
                content_digest=_digest("existing"),
            ),
            TaskTreeEntry(
                path="tests/test_existing.py",
                git_mode="100644",
                blob_oid=_oid("test-existing"),
                size=1,
                content_digest=_digest("test-existing"),
            ),
        )

    def test_existing_file_and_missing_leaf_are_classified(self):
        self.assertEqual(
            _classify_allowed_write_paths(
                ("src/existing.py", "src/new.py"), self.entries
            ),
            (
                {
                    "disposition": "existing_regular_file",
                    "path": "src/existing.py",
                },
                {"disposition": "missing_leaf", "path": "src/new.py"},
            ),
        )

    def test_empty_allowed_write_list_is_valid(self):
        self.assertEqual(
            _classify_allowed_write_paths((), self.entries),
            (),
        )

    def test_policy_digest_binds_exact_transient_document(self):
        records = _classify_allowed_write_paths(
            ("src/existing.py", "src/new.py"), self.entries
        )
        expected = {
            "allowed_write_paths": [
                {
                    "disposition": "existing_regular_file",
                    "path": "src/existing.py",
                },
                {"disposition": "missing_leaf", "path": "src/new.py"},
            ],
            "document_type": "task-allowed-write-policy-v1",
            "materialized_tree_digest": _digest("tree"),
            "schema_version": 1,
            "snapshot_receipt_digest": _digest("snapshot"),
            "task_id": "low-alpha",
        }
        self.assertEqual(
            _allowed_write_policy_digest(
                task_id="low-alpha",
                materialized_tree_digest=_digest("tree"),
                snapshot_receipt_digest=_digest("snapshot"),
                records=records,
            ),
            "sha256:"
            + hashlib.sha256(canonical_bytes(expected)).hexdigest(),
        )

    def test_directory_missing_intermediate_alias_and_exclusion_are_rejected(self):
        invalid = (
            ("src",),
            ("missing/new.py",),
            ("SRC/existing.py",),
            ("src/.env",),
        )
        for paths in invalid:
            with self.subTest(paths=paths):
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^task_snapshot_preflight_blocked$",
                ):
                    _classify_allowed_write_paths(paths, self.entries)


class ExperimentRequestContractTests(unittest.TestCase):
    def setUp(self):
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "harness_experiment"
            / "valid-plan-input.json"
        )
        self.experiment_input = load_experiment_input(fixture.read_bytes())

    def _source(self, task_id, root, commit_oid):
        return TaskSourceSpec(
            input_digest=self.experiment_input.input_digest,
            task_id=task_id,
            repository_root=root,
            commit_oid=commit_oid,
            provisioning_class=(
                "operator_owned_trusted_git_local_clone"
            ),
            operator_attested=True,
            local_clone_policy="remote_or_no_local_or_no_hardlinks",
        )

    def test_request_has_exact_fields_and_redacts_paths_from_repr(self):
        candidates = tuple(self.experiment_input.value["candidates"])
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            bundle = base / "bundle"
            skill = base / "skill"
            temp_parent = base / "temp"
            source_root = base / "source"
            for path in (bundle, skill, temp_parent, source_root):
                path.mkdir(mode=0o700)
            sources = {
                candidate["task_id"]: self._source(
                    candidate["task_id"],
                    source_root,
                    candidate["commit_oid"],
                )
                for candidate in candidates
            }
            request = ExperimentPreflightRequest(
                experiment_input=self.experiment_input,
                bundle_root=bundle,
                skill_repo=skill,
                temp_parent=temp_parent,
                task_sources=sources,
            )
            self.assertEqual(
                tuple(item.name for item in fields(request)),
                (
                    "experiment_input",
                    "bundle_root",
                    "skill_repo",
                    "temp_parent",
                    "task_sources",
                ),
            )
            rendered = repr(request)
            self.assertNotIn(str(base), rendered)
            self.assertIsInstance(request.task_sources, MappingProxyType)

    def test_request_rejects_non_exact_absolute_path_spelling(self):
        candidate = tuple(self.experiment_input.value["candidates"])[0]
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            good = base / "good"
            good.mkdir()
            source = self._source(
                candidate["task_id"], good, candidate["commit_oid"]
            )
            for bad in (
                Path("relative"),
                Path(str(base) + "/../" + base.name),
                str(base) + "/",
            ):
                with self.subTest(bad=bad):
                    with self.assertRaisesRegex(
                        ExperimentPreflightError,
                        "^experiment_preflight_invalid$",
                    ):
                        ExperimentPreflightRequest(
                            experiment_input=self.experiment_input,
                            bundle_root=bad,
                            skill_repo=good,
                            temp_parent=good,
                            task_sources={candidate["task_id"]: source},
                        )

    def test_invalid_public_request_is_blocked_before_any_write(self):
        result = run_experiment_preflight(object())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.cleanup_state, "not_started")
        self.assertEqual(
            result.reason_code, "experiment_preflight_invalid"
        )
        self.assertEqual(result.model_calls, 0)


class OwnershipAndCleanupTests(unittest.TestCase):
    def _directory(self, parent, name):
        path = parent / name
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        return path

    def test_physical_gate_requires_empty_private_parent_and_disjoint_roots(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            protected = self._directory(base, "protected")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                _require_physical_disjointness(gate, (protected,))
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^experiment_preflight_invalid$",
                ):
                    _require_physical_disjointness(
                        gate, (temp_parent / "future-child",)
                    )
            finally:
                gate.close()

            (temp_parent / "unexpected").write_text("x")
            with self.assertRaisesRegex(
                ExperimentPreflightError,
                "^experiment_preflight_invalid$",
            ):
                _open_physical_directory_gate(temp_parent)

    def test_handoff_publishes_one_exact_metadata_inventory_commit(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("homes",))
                _create_owned_directory(ledger, ("homes", "current"))
                handoff = _begin_handoff(
                    ledger, (("homes", "current"),)
                )
                target = (
                    temp_parent / "phase-a" / "homes" / "current"
                )
                (target / "AGENTS.md").write_text("sealed")
                os.chmod(target / "AGENTS.md", 0o400)
                published = _complete_handoff(
                    ledger, handoff, lambda: None
                )
                self.assertIn(
                    ("homes", "current", "AGENTS.md"), published
                )
                entry = published[
                    ("homes", "current", "AGENTS.md")
                ]
                self.assertEqual(entry.mode, 0o400)
                self.assertEqual(entry.nlink, 1)
                self.assertEqual(entry.size, 6)
                self.assertIsInstance(entry.mtime_ns, int)
                self.assertIsInstance(entry.ctime_ns, int)
                self.assertFalse(ledger.has_pending_handoffs)
                self.assertTrue(_cleanup_owned_phase(ledger))
                self.assertFalse((temp_parent / "phase-a").exists())
                self.assertTrue(temp_parent.is_dir())
            finally:
                gate.close()

    def test_failed_handoff_is_quarantined_and_cleanup_does_not_invent_authority(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("home",))
                handoff = _begin_handoff(ledger, (("home",),))
                target = temp_parent / "phase-a" / "home"
                (target / "partial").write_text("data")
                with self.assertRaisesRegex(RuntimeError, "^verify failed$"):
                    _complete_handoff(
                        ledger,
                        handoff,
                        lambda: (_ for _ in ()).throw(
                            RuntimeError("verify failed")
                        ),
                    )
                before_mode = stat.S_IMODE(target.stat().st_mode)
                with mock.patch(
                    "scripts.live_eval.experiment.os.unlink"
                ) as unlink, mock.patch(
                    "scripts.live_eval.experiment.os.rmdir"
                ) as rmdir, mock.patch(
                    "scripts.live_eval.experiment.os.fchmod"
                ) as chmod:
                    self.assertFalse(_cleanup_owned_phase(ledger))
                unlink.assert_not_called()
                rmdir.assert_not_called()
                chmod.assert_not_called()
                self.assertEqual(
                    stat.S_IMODE(target.stat().st_mode), before_mode
                )
                self.assertTrue(target.exists())
            finally:
                gate.close()

    def test_metadata_mutation_blocks_cleanup_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("task",))
                handoff = _begin_handoff(ledger, (("task",),))
                target = temp_parent / "phase-a" / "task"
                file_path = target / "result.txt"
                file_path.write_text("before")
                _complete_handoff(ledger, handoff, lambda: None)
                file_path.write_text("after!")
                with mock.patch(
                    "scripts.live_eval.experiment.os.unlink"
                ) as unlink, mock.patch(
                    "scripts.live_eval.experiment.os.rmdir"
                ) as rmdir, mock.patch(
                    "scripts.live_eval.experiment.os.fchmod"
                ) as chmod:
                    self.assertFalse(_cleanup_owned_phase(ledger))
                unlink.assert_not_called()
                rmdir.assert_not_called()
                chmod.assert_not_called()
                self.assertTrue(file_path.exists())
            finally:
                gate.close()

    def test_hard_link_is_rejected_before_handoff_publication(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            outside = self._directory(base, "outside")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("task",))
                handoff = _begin_handoff(ledger, (("task",),))
                source = outside / "source"
                source.write_text("x")
                os.link(
                    source,
                    temp_parent / "phase-a" / "task" / "linked",
                )
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^task_snapshot_preflight_blocked$",
                ):
                    _complete_handoff(ledger, handoff, lambda: None)
                self.assertTrue(ledger.has_pending_handoffs)
            finally:
                gate.close()

    def test_same_batch_metadata_mutation_blocks_publication(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("home",))
                target = temp_parent / "phase-a" / "home"
                file_path = target / "sealed"
                file_path.write_text("first")
                handoff = _begin_handoff(ledger, (("home",),))

                def mutate_during_verification():
                    file_path.write_text("later")

                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^task_snapshot_preflight_blocked$",
                ):
                    _complete_handoff(
                        ledger, handoff, mutate_during_verification
                    )
                self.assertTrue(ledger.has_pending_handoffs)
                self.assertFalse(_cleanup_owned_phase(ledger))
                self.assertTrue(file_path.exists())
            finally:
                gate.close()

    def test_empty_handoff_does_not_rebaseline_changed_directory_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("home",))
                handoff = _begin_handoff(ledger, (("home",),))
                transient = temp_parent / "phase-a" / "home" / "transient"
                transient.write_text("x")
                transient.unlink()
                self.assertFalse(
                    _resolve_empty_handoff(ledger, handoff)
                )
                self.assertTrue(ledger.has_pending_handoffs)
                self.assertFalse(_cleanup_owned_phase(ledger))
            finally:
                gate.close()

    def test_handoff_rejects_replaced_precreated_root_token(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("home",))
                handoff = _begin_handoff(ledger, (("home",),))
                target = temp_parent / "phase-a" / "home"
                original_inode = target.stat().st_ino
                target.rmdir()
                target.mkdir(mode=0o700)
                os.chmod(target, 0o700)
                self.assertNotEqual(target.stat().st_ino, original_inode)
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^task_snapshot_preflight_blocked$",
                ):
                    _complete_handoff(
                        ledger, handoff, lambda: None
                    )
                self.assertTrue(ledger.has_pending_handoffs)
                self.assertFalse(_cleanup_owned_phase(ledger))
                self.assertTrue(target.is_dir())
            finally:
                gate.close()

    def test_created_directory_does_not_acquire_injected_descendant(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                real_mkdir = os.mkdir

                def mkdir_then_inject(name, mode=0o777, *, dir_fd=None):
                    real_mkdir(name, mode=mode, dir_fd=dir_fd)
                    if name != "owned":
                        return
                    child_descriptor = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=dir_fd,
                    )
                    try:
                        foreign_descriptor = os.open(
                            "foreign",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=child_descriptor,
                        )
                        os.close(foreign_descriptor)
                    finally:
                        os.close(child_descriptor)

                with mock.patch(
                    "scripts.live_eval.experiment.os.mkdir",
                    side_effect=mkdir_then_inject,
                ):
                    with self.assertRaisesRegex(
                        ExperimentPreflightError,
                        "^task_snapshot_preflight_blocked$",
                    ):
                        _create_owned_directory(ledger, ("owned",))
                self.assertTrue(ledger.has_pending_handoffs)
                self.assertNotIn(("owned", "foreign"), ledger.entries)
                self.assertFalse(_cleanup_owned_phase(ledger))
                self.assertTrue(
                    (
                        temp_parent
                        / "phase-a"
                        / "owned"
                        / "foreign"
                    ).is_file()
                )
            finally:
                gate.close()

    def test_created_directory_rejects_post_capture_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            outside = self._directory(base, "outside")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                real_complete = experiment_module._complete_handoff

                def replace_then_complete(
                    checked_ledger,
                    handoff,
                    verifier,
                    *arguments,
                    **keywords
                ):
                    target = temp_parent / "phase-a" / "owned"
                    target.rename(outside / "created-owned")
                    target.mkdir(mode=0o700)
                    return real_complete(
                        checked_ledger,
                        handoff,
                        verifier,
                        *arguments,
                        **keywords
                    )

                with mock.patch(
                    "scripts.live_eval.experiment._complete_handoff",
                    side_effect=replace_then_complete,
                ):
                    with self.assertRaisesRegex(
                        ExperimentPreflightError,
                        "^task_snapshot_preflight_blocked$",
                    ):
                        _create_owned_directory(ledger, ("owned",))
                self.assertTrue(ledger.has_pending_handoffs)
                self.assertNotIn(("owned",), ledger.entries)
                self.assertFalse(_cleanup_owned_phase(ledger))
                self.assertTrue((outside / "created-owned").is_dir())
                self.assertTrue(
                    (temp_parent / "phase-a" / "owned").is_dir()
                )
            finally:
                gate.close()

    def test_created_directory_close_fault_does_not_reclose_or_leak_parent(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                real_open = os.open
                real_open_relative = (
                    experiment_module._open_relative_directory
                )
                real_close_descriptor = (
                    experiment_module._close_descriptor
                )
                opened = {"child": -1, "parent": -1}
                faulted = {"value": False}

                def record_parent(root_descriptor, components):
                    descriptor = real_open_relative(
                        root_descriptor,
                        components,
                    )
                    if components[-1] == "phase-a":
                        opened["parent"] = descriptor
                    return descriptor

                def record_child(name, flags, *arguments, **keywords):
                    descriptor = real_open(
                        name,
                        flags,
                        *arguments,
                        **keywords,
                    )
                    if name == "owned" and opened["child"] < 0:
                        opened["child"] = descriptor
                    return descriptor

                def close_child_then_fault(descriptor):
                    result = real_close_descriptor(descriptor)
                    if (
                        descriptor == opened["child"]
                        and not faulted["value"]
                    ):
                        faulted["value"] = True
                        raise RuntimeError("child close fault")
                    return result

                with mock.patch(
                    "scripts.live_eval.experiment."
                    "_open_relative_directory",
                    side_effect=record_parent,
                ), mock.patch(
                    "scripts.live_eval.experiment.os.open",
                    side_effect=record_child,
                ), mock.patch(
                    "scripts.live_eval.experiment._close_descriptor",
                    side_effect=close_child_then_fault,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "^child close fault$",
                    ):
                        _create_owned_directory(ledger, ("owned",))

                self.assertTrue(faulted["value"])
                for descriptor in (
                    opened["child"],
                    opened["parent"],
                ):
                    self.assertGreaterEqual(descriptor, 0)
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
            finally:
                gate.close()

    def test_created_directory_rejects_post_capture_metadata_change(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                real_bounded = (
                    experiment_module._bounded_directory_names
                )
                mutated = {"value": False}

                def scan_then_mutate(descriptor, *, maximum):
                    names = real_bounded(
                        descriptor,
                        maximum=maximum,
                    )
                    if maximum == 0 and not mutated["value"]:
                        mutated["value"] = True
                        transient = os.open(
                            "transient",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=descriptor,
                        )
                        os.close(transient)
                        os.unlink("transient", dir_fd=descriptor)
                    return names

                with mock.patch(
                    "scripts.live_eval.experiment."
                    "_bounded_directory_names",
                    side_effect=scan_then_mutate,
                ):
                    with self.assertRaisesRegex(
                        ExperimentPreflightError,
                        "^task_snapshot_preflight_blocked$",
                    ):
                        _create_owned_directory(ledger, ("owned",))
                self.assertTrue(mutated["value"])
                self.assertTrue(ledger.has_pending_handoffs)
                self.assertNotIn(("owned",), ledger.entries)
                self.assertFalse(_cleanup_owned_phase(ledger))
            finally:
                gate.close()

    def test_expected_handoff_entry_must_be_inside_handoff_scope(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("owned",))
                handoff = _begin_handoff(ledger, (("owned",),))
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^task_snapshot_preflight_blocked$",
                ):
                    _complete_handoff(
                        ledger,
                        handoff,
                        lambda: None,
                        expected_entries={
                            (): ledger.entries[()],
                        },
                    )
                self.assertTrue(ledger.has_pending_handoffs)
                self.assertFalse(_cleanup_owned_phase(ledger))
            finally:
                gate.close()

    def test_owned_entry_ceiling_fails_closed_before_publication(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("home",))
                handoff = _begin_handoff(ledger, (("home",),))
                target = temp_parent / "phase-a" / "home"
                (target / "one").write_text("1")
                (target / "two").write_text("2")
                with mock.patch(
                    "scripts.live_eval.experiment."
                    "_MAX_OWNED_LEDGER_ENTRIES",
                    3,
                ):
                    with self.assertRaisesRegex(
                        ExperimentPreflightError,
                        "^task_snapshot_preflight_blocked$",
                    ):
                        _complete_handoff(
                            ledger, handoff, lambda: None
                        )
                self.assertTrue(ledger.has_pending_handoffs)
            finally:
                gate.close()

    def test_file_stat_to_unlink_swap_cannot_report_cleanup_success(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            outside = self._directory(base, "outside")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                _create_owned_directory(ledger, ("task",))
                handoff = _begin_handoff(ledger, (("task",),))
                target = temp_parent / "phase-a" / "task"
                (target / "result.txt").write_text("owned")
                _complete_handoff(ledger, handoff, lambda: None)
                real_unlink = os.unlink
                swapped = {"done": False}

                def swap_then_unlink(name, *, dir_fd=None):
                    if name == "result.txt" and not swapped["done"]:
                        swapped["done"] = True
                        os.rename(
                            name,
                            outside / "owned-preserved",
                            src_dir_fd=dir_fd,
                        )
                        descriptor = os.open(
                            name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=dir_fd,
                        )
                        try:
                            os.write(descriptor, b"replacement")
                        finally:
                            os.close(descriptor)
                    return real_unlink(name, dir_fd=dir_fd)

                with mock.patch(
                    "scripts.live_eval.experiment.os.unlink",
                    side_effect=swap_then_unlink,
                ):
                    self.assertFalse(_cleanup_owned_phase(ledger))
                self.assertTrue(swapped["done"])
                self.assertTrue((outside / "owned-preserved").is_file())
            finally:
                gate.close()

    def test_directory_and_root_rmdir_swaps_cannot_report_cleanup_success(self):
        for swapped_name in ("task", "phase-a"):
            with self.subTest(swapped_name=swapped_name):
                with tempfile.TemporaryDirectory() as root:
                    base = Path(root).resolve()
                    temp_parent = self._directory(base, "temp")
                    outside = self._directory(base, "outside")
                    gate = _open_physical_directory_gate(temp_parent)
                    try:
                        ledger = _create_phase_ledger(gate)
                        _create_owned_directory(ledger, ("task",))
                        real_rmdir = os.rmdir
                        swapped = {"done": False}

                        def swap_then_rmdir(name, *, dir_fd=None):
                            if (
                                name == swapped_name
                                and not swapped["done"]
                            ):
                                swapped["done"] = True
                                os.rename(
                                    name,
                                    outside / ("owned-" + swapped_name),
                                    src_dir_fd=dir_fd,
                                )
                                os.mkdir(
                                    name,
                                    mode=0o700,
                                    dir_fd=dir_fd,
                                )
                            return real_rmdir(name, dir_fd=dir_fd)

                        with mock.patch(
                            "scripts.live_eval.experiment.os.rmdir",
                            side_effect=swap_then_rmdir,
                        ):
                            self.assertFalse(
                                _cleanup_owned_phase(ledger)
                            )
                        self.assertTrue(swapped["done"])
                        self.assertTrue(
                            (
                                outside / ("owned-" + swapped_name)
                            ).is_dir()
                        )
                    finally:
                        gate.close()

    def test_cleanup_normalizes_ordinary_exception_and_propagates_abort(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                with mock.patch(
                    "scripts.live_eval.experiment.os.fchmod",
                    side_effect=RuntimeError("ordinary cleanup fault"),
                ):
                    self.assertFalse(_cleanup_owned_phase(ledger))
            finally:
                gate.close()

        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                with mock.patch(
                    "scripts.live_eval.experiment.os.fchmod",
                    side_effect=_Abort("cleanup abort"),
                ):
                    with self.assertRaises(_Abort):
                        _cleanup_owned_phase(ledger)
            finally:
                gate.close()

    def test_close_fault_does_not_mask_active_cleanup_abort(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                real_close = os.close
                abort_active = {"value": False}

                def aborting_chmod(*unused_args, **unused_kwargs):
                    abort_active["value"] = True
                    raise _Abort("primary abort")

                def close_then_fail(descriptor):
                    real_close(descriptor)
                    if abort_active["value"]:
                        raise RuntimeError("secondary close fault")

                with mock.patch(
                    "scripts.live_eval.experiment.os.fchmod",
                    side_effect=aborting_chmod,
                ), mock.patch(
                    "scripts.live_eval.experiment.os.close",
                    side_effect=close_then_fail,
                ):
                    with self.assertRaisesRegex(
                        _Abort, "^primary abort$"
                    ):
                        _cleanup_owned_phase(ledger)
            finally:
                gate.close()

    def test_close_abort_is_not_swallowed_by_active_ordinary_error(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                ledger = _create_phase_ledger(gate)
                real_close = os.close
                ordinary_active = {"value": False}

                def ordinary_chmod(*unused_args, **unused_kwargs):
                    ordinary_active["value"] = True
                    raise RuntimeError("primary ordinary fault")

                def close_then_abort(descriptor):
                    real_close(descriptor)
                    if ordinary_active["value"]:
                        raise _Abort("terminal close abort")

                with mock.patch(
                    "scripts.live_eval.experiment.os.fchmod",
                    side_effect=ordinary_chmod,
                ), mock.patch(
                    "scripts.live_eval.experiment.os.close",
                    side_effect=close_then_abort,
                ):
                    with self.assertRaisesRegex(
                        _Abort, "^terminal close abort$"
                    ):
                        _cleanup_owned_phase(ledger)
            finally:
                gate.close()

    def test_descriptor_operations_do_not_depend_on_set_inheritable(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                with mock.patch(
                    "scripts.live_eval.experiment.os.set_inheritable",
                    side_effect=AssertionError(
                        "set_inheritable must not be called"
                    ),
                ):
                    ledger = _create_phase_ledger(gate)
                    _create_owned_directory(ledger, ("owned",))
                    self.assertTrue(_cleanup_owned_phase(ledger))
            finally:
                gate.close()

    def test_absolute_chain_closes_new_descriptor_when_fstat_fails(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            target = self._directory(base, "fstat-failure-target")
            real_open = os.open
            real_fstat = os.fstat
            opened_target = {"descriptor": -1}

            def record_target_open(path, flags, *args, **kwargs):
                descriptor = real_open(path, flags, *args, **kwargs)
                if path == target.name:
                    opened_target["descriptor"] = descriptor
                return descriptor

            def fail_target_fstat(descriptor):
                if descriptor == opened_target["descriptor"]:
                    raise RuntimeError("target fstat fault")
                return real_fstat(descriptor)

            with mock.patch(
                "scripts.live_eval.experiment.os.open",
                side_effect=record_target_open,
            ), mock.patch(
                "scripts.live_eval.experiment.os.fstat",
                side_effect=fail_target_fstat,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "^target fstat fault$"
                ):
                    experiment_module._open_absolute_directory_chain(
                        target
                    )

            descriptor = opened_target["descriptor"]
            self.assertGreaterEqual(descriptor, 0)
            try:
                real_fstat(descriptor)
            except OSError:
                descriptor_closed = True
            else:
                descriptor_closed = False
                os.close(descriptor)
            self.assertTrue(descriptor_closed)

    def test_physical_gate_rejects_untrusted_writable_ancestor(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            unsafe = self._directory(base, "unsafe")
            os.chmod(unsafe, 0o777)
            temp_parent = self._directory(unsafe, "temp")
            with self.assertRaisesRegex(
                ExperimentPreflightError,
                "^experiment_preflight_invalid$",
            ):
                _open_physical_directory_gate(temp_parent)

    def test_physical_gate_allows_unrelated_ancestor_inventory_change(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            ancestor = self._directory(base, "ancestor")
            temp_parent = self._directory(ancestor, "temp")
            real_open = os.open
            mutated = {"value": False}

            def open_then_add_unrelated(path, flags, *args, **kwargs):
                descriptor = real_open(path, flags, *args, **kwargs)
                if path == "temp" and not mutated["value"]:
                    mutated["value"] = True
                    sibling = real_open(
                        "unrelated",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=kwargs["dir_fd"],
                    )
                    os.close(sibling)
                return descriptor

            with mock.patch(
                "scripts.live_eval.experiment.os.open",
                side_effect=open_then_add_unrelated,
            ):
                gate = _open_physical_directory_gate(temp_parent)
            try:
                self.assertTrue(mutated["value"])
                self.assertEqual(gate.path, temp_parent)
            finally:
                gate.close()
                (ancestor / "unrelated").unlink()

    def test_physical_gate_rejects_ancestor_mode_change_after_capture(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            ancestor = self._directory(base, "ancestor")
            temp_parent = self._directory(ancestor, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            try:
                os.chmod(ancestor, 0o755)
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^experiment_preflight_invalid$",
                ):
                    gate.require_live()
            finally:
                os.chmod(ancestor, 0o700)
                gate.close()

    def test_physical_gate_rejects_intermediate_path_rebind(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            ancestor = self._directory(base, "ancestor")
            temp_parent = self._directory(ancestor, "temp")
            gate = _open_physical_directory_gate(temp_parent)
            moved = base / "moved-ancestor"
            try:
                ancestor.rename(moved)
                ancestor.symlink_to(moved, target_is_directory=True)
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^experiment_preflight_invalid$",
                ):
                    gate.require_live()
            finally:
                gate.close()

    def test_physical_gate_closes_descriptor_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            temp_parent = self._directory(base, "temp")
            real_chain = experiment_module._open_absolute_directory_chain
            real_fstat = os.fstat
            opened = {"descriptor": -1}

            def record_chain(path):
                result = real_chain(path)
                opened["descriptor"] = result[0]
                return result

            with mock.patch(
                "scripts.live_eval.experiment."
                "_open_absolute_directory_chain",
                side_effect=record_chain,
            ), mock.patch(
                "scripts.live_eval.experiment._directory_is_empty",
                side_effect=ExperimentPreflightError(
                    "experiment_preflight_invalid"
                ),
            ):
                with self.assertRaisesRegex(
                    ExperimentPreflightError,
                    "^experiment_preflight_invalid$",
                ):
                    _open_physical_directory_gate(temp_parent)

            descriptor = opened["descriptor"]
            self.assertGreaterEqual(descriptor, 0)
            try:
                real_fstat(descriptor)
            except OSError:
                descriptor_closed = True
            else:
                descriptor_closed = False
                os.close(descriptor)
            self.assertTrue(descriptor_closed)

    def test_phase_exists_treats_file_and_symlink_as_residual(self):
        for replacement_kind in ("file", "symlink"):
            with self.subTest(replacement_kind=replacement_kind):
                with tempfile.TemporaryDirectory() as root:
                    base = Path(root).resolve()
                    temp_parent = self._directory(base, "temp")
                    outside = self._directory(base, "outside")
                    gate = _open_physical_directory_gate(temp_parent)
                    try:
                        phase = temp_parent / "phase-a"
                        if replacement_kind == "file":
                            phase.write_text("replacement")
                        else:
                            phase.symlink_to(outside)
                        self.assertTrue(
                            experiment_module._phase_exists(gate)
                        )
                    finally:
                        gate.close()


class FullPreflightIntegrationTests(unittest.TestCase):
    _SKILLS = (
        "adversarial-review-loop",
        "workflow",
        "workflow-intake",
    )
    _ROLES = (
        "accessibility-reviewer",
        "api-reviewer",
        "architect",
        "code-reviewer",
        "dba",
        "designer",
        "developer",
        "documenter",
        "explorer",
        "performance-reviewer",
        "pm",
        "publisher",
        "qa-engineer",
        "security-reviewer",
        "test-coverage-reviewer",
        "ux-reviewer",
    )

    def _git(self, repo, *arguments):
        return subprocess.run(
            ("git",) + arguments,
            cwd=str(repo),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def _make_skill_repo(self, repo):
        repo.mkdir(mode=0o700)
        (repo / ".codex-plugin").mkdir()
        (repo / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "harness-test",
                    "skills": "./skills/",
                    "version": "1.0.0",
                }
            )
            + "\n"
        )
        for index, name in enumerate(self._SKILLS):
            skill = repo / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: {}\n---\npolicy {}\n".format(name, index)
            )
        self._git(repo, "init", "-q")
        self._git(repo, "add", ".")
        self._git(
            repo,
            "-c",
            "user.name=Harness",
            "-c",
            "user.email=harness@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )

    def _make_bundle(self, root):
        (root / "profiles" / "current").mkdir(
            parents=True, mode=0o700
        )
        (root / "profiles" / "lean").mkdir(mode=0o700)
        (root / "shared" / "agents").mkdir(
            parents=True, mode=0o700
        )
        (root / "shared" / "common-agents").mkdir(mode=0o700)
        files = {
            "harness.json": json.dumps(
                {"bundle_id": "fixture-v1", "schema_version": 1},
                sort_keys=True,
                separators=(",", ":"),
            ),
            "profiles/current/AGENTS.md": "# Current\nComplete policy.\n",
            "profiles/lean/AGENTS.md": "# Lean\nCompact policy.\n",
        }
        for role in self._ROLES:
            files["shared/agents/{}.toml".format(role)] = (
                'name = "{}"\n'.format(role)
                + 'description = "Common {} role. Uses '
                '~/.agents/common-agents/{}.md."\n'.format(role, role)
                + 'model_reasoning_effort = "medium"\n'
                + 'developer_instructions = """\n'
                + "# {} Adapter\n\n".format(role)
                + "Before acting, read and follow "
                + "`/private/fixture/.agents/common-agents/"
                + "{}.md`.\n\n".format(role)
                + "Project-local instructions override this adapter.\n"
                + '"""\n'
            )
            files["shared/common-agents/{}.md".format(role)] = (
                "# {}\nRole policy.\n".format(role)
            )
        for relative, content in files.items():
            path = root / relative
            path.write_text(content)
            os.chmod(path, 0o600)
        for path in (root,) + tuple(
            item for item in root.rglob("*") if item.is_dir()
        ):
            os.chmod(path, 0o700)

    def _make_task_repo(self, repo, allowed_paths):
        repo.mkdir(mode=0o700)
        self._git(repo, "init", "-q")
        for relative in allowed_paths:
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# fixture {}\nLOCAL-SENTINEL-DO-NOT-LEAK\n".format(
                    relative
                )
            )
        self._git(repo, "add", ".")
        self._git(
            repo,
            "-c",
            "user.name=Task",
            "-c",
            "user.email=task@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        return self._git(repo, "rev-parse", "HEAD")

    def _make_request_fixture(self, base, temp_name="temp-parent"):
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "harness_experiment"
            / "valid-plan-input.json"
        )
        document = json.loads(fixture.read_text())
        bundle = base / "bundle"
        skill_repo = base / "skill-repo"
        temp_parent = base / temp_name
        temp_parent.mkdir(mode=0o700)
        os.chmod(temp_parent, 0o700)
        self._make_bundle(bundle)
        self._make_skill_repo(skill_repo)
        repositories = {}
        for candidate in document["candidates"]:
            repo = base / ("source-" + candidate["task_id"])
            candidate["commit_oid"] = self._make_task_repo(
                repo, candidate["allowed_write_paths"]
            )
            repositories[candidate["task_id"]] = repo
        experiment_input = load_experiment_input(
            canonical_bytes(document)
        )
        sources = {
            candidate["task_id"]: TaskSourceSpec(
                input_digest=experiment_input.input_digest,
                task_id=candidate["task_id"],
                repository_root=repositories[candidate["task_id"]],
                commit_oid=candidate["commit_oid"],
                provisioning_class=candidate[
                    "source_provisioning_class"
                ],
                operator_attested=candidate["operator_attested"],
                local_clone_policy=candidate["local_clone_policy"],
            )
            for candidate in document["candidates"]
        }
        return (
            ExperimentPreflightRequest(
                experiment_input=experiment_input,
                bundle_root=bundle,
                skill_repo=skill_repo,
                temp_parent=temp_parent,
                task_sources=sources,
            ),
            document,
            repositories,
        )

    def _cli_arguments(self, request, plan_path, repositories):
        arguments = [
            "preflight",
            "--input",
            str(plan_path),
            "--bundle-root",
            str(request.bundle_root),
            "--skill-repo",
            str(request.skill_repo),
            "--temp-parent",
            str(request.temp_parent),
        ]
        for task_id in sorted(repositories):
            arguments.extend(
                (
                    "--task-source",
                    "{}={}".format(task_id, repositories[task_id]),
                )
            )
        return arguments

    def _run_cli(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = experiment_cli.main(arguments)
        return return_code, stdout.getvalue(), stderr.getvalue()

    def _assert_fixed_blocked_cli_output(self, output):
        self.assertEqual(output.count("\n"), 1)
        self.assertTrue(output.endswith("\n"))
        payload = json.loads(output)
        self.assertEqual(
            payload,
            {
                "bundle_digest": None,
                "cleanup_state": "not_started",
                "current_profile_digest": None,
                "global_agents_marker_state": (
                    "global_agents_marker_not_run"
                ),
                "lean_profile_digest": None,
                "live_backend_state": "live_backend_not_implemented",
                "materialization_result": "blocked",
                "model_calls": 0,
                "pilot_state": "pilot_not_run",
                "plan_digest": None,
                "preflight_receipt_digest": None,
                "qualification_evidence_classification": "not_validated",
                "reason_code": "experiment_preflight_invalid",
                "status": "blocked",
                "task_corpus_receipt_digest": None,
            },
        )
        self.assertEqual(
            output,
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n",
        )

    def test_cli_parser_surface_is_exactly_preflight_and_static_options(self):
        parser = experiment_cli._build_parser()
        self.assertEqual(
            {
                option
                for action in parser._actions
                for option in action.option_strings
            },
            {"-h", "--help"},
        )
        subparsers = tuple(
            action
            for action in parser._actions
            if isinstance(action, experiment_cli.argparse._SubParsersAction)
        )
        self.assertEqual(len(subparsers), 1)
        self.assertEqual(set(subparsers[0].choices), {"preflight"})
        preflight = subparsers[0].choices["preflight"]
        option_actions = tuple(
            action
            for action in preflight._actions
            if action.option_strings
        )
        self.assertEqual(
            {
                option
                for action in option_actions
                for option in action.option_strings
            },
            {
                "-h",
                "--help",
                "--input",
                "--bundle-root",
                "--skill-repo",
                "--temp-parent",
                "--task-source",
            },
        )
        self.assertEqual(
            {
                action.option_strings[-1]
                for action in option_actions
                if action.required
            },
            {
                "--input",
                "--bundle-root",
                "--skill-repo",
                "--temp-parent",
                "--task-source",
            },
        )
        task_source = next(
            action
            for action in option_actions
            if "--task-source" in action.option_strings
        )
        self.assertIsInstance(
            task_source, experiment_cli.argparse._AppendAction
        )

    def test_cli_direct_script_bootstrap_and_entrypoint_are_sanitized(self):
        root = Path(__file__).parents[1]
        script = root / "scripts" / "run_harness_experiment.py"
        completed = subprocess.run(
            (sys.executable, str(script), "canary"),
            cwd=str(root),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        self._assert_fixed_blocked_cli_output(completed.stdout)
        self.assertNotIn(str(root), completed.stdout)

        with mock.patch.object(
            experiment_cli,
            "main",
            side_effect=KeyboardInterrupt(),
        ):
            self.assertEqual(experiment_cli._entrypoint(()), 130)

    def test_cli_fixture_backed_preflight_is_static_only(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve() / "PRIVATE-CLI-PATH"
            base.mkdir(mode=0o700)
            request, unused_document, repositories = (
                self._make_request_fixture(base)
            )
            plan_path = base / "private-plan.json"
            plan_path.write_bytes(
                request.experiment_input.canonical_bytes
            )
            arguments = self._cli_arguments(
                request, plan_path, repositories
            )

            return_code, stdout, stderr = self._run_cli(arguments)

            self.assertEqual(return_code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(stdout.count("\n"), 1)
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "static_only")
            self.assertEqual(
                payload["live_backend_state"],
                "live_backend_not_implemented",
            )
            self.assertEqual(
                payload["global_agents_marker_state"],
                "global_agents_marker_not_run",
            )
            self.assertEqual(payload["pilot_state"], "pilot_not_run")
            self.assertEqual(
                payload["qualification_evidence_classification"],
                "operator_attested_static",
            )
            self.assertEqual(payload["model_calls"], 0)
            self.assertEqual(
                payload["materialization_result"], "verified"
            )
            self.assertEqual(payload["cleanup_state"], "removed")
            self.assertEqual(
                payload["reason_code"], "static_preflight_verified"
            )
            self.assertEqual(
                stdout,
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                + "\n",
            )
            captured = stdout + stderr
            for private_value in (
                str(base),
                str(plan_path),
                str(request.bundle_root),
                str(request.skill_repo),
                str(request.temp_parent),
                *(str(path) for path in repositories.values()),
                "LOCAL-SENTINEL-DO-NOT-LEAK",
            ):
                self.assertNotIn(private_value, captured)
            self.assertEqual(tuple(request.temp_parent.iterdir()), ())

    def test_cli_rejects_live_commands_and_options_before_any_read_or_run(self):
        private_path = "/private/SYNTHETIC-CLI-PATH"
        secret = "synthetic-secret-value"
        valid_shape = (
            "preflight",
            "--input",
            private_path,
            "--bundle-root",
            private_path,
            "--skill-repo",
            private_path,
            "--temp-parent",
            private_path,
            "--task-source",
            "low-alpha=" + private_path,
            "--task-source",
            "low-beta=" + private_path,
            "--task-source",
            "medium-alpha=" + private_path,
            "--task-source",
            "medium-beta=" + private_path,
        )
        cases = (
            ("canary",),
            ("pilot",),
            ("canary", "--api-key", secret),
            ("pilot", "--approve-pilot", secret),
            valid_shape + ("--api-key", secret),
            valid_shape + ("--codex-executable", secret),
            valid_shape + ("--live-ledger", secret),
            valid_shape + ("--approve-pilot",),
            valid_shape + ("--help", "--api-key", secret),
            ("--help", "canary"),
            (
                valid_shape[0],
                "--inp",
                *valid_shape[2:],
            ),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), mock.patch.object(
                experiment_cli,
                "_read_stable_input_file",
            ) as reader, mock.patch.object(
                experiment_cli,
                "run_experiment_preflight",
            ) as runner:
                return_code, stdout, stderr = self._run_cli(arguments)
                self.assertEqual(return_code, 2)
                self.assertEqual(stderr, "")
                self._assert_fixed_blocked_cli_output(stdout)
                self.assertEqual(reader.call_count, 0)
                self.assertEqual(runner.call_count, 0)
                self.assertNotIn(secret, stdout + stderr)
                self.assertNotIn(private_path, stdout + stderr)

    def test_cli_rejects_invalid_task_source_bindings_before_orchestration(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, repositories = (
                self._make_request_fixture(base)
            )
            plan_path = base / "plan.json"
            plan_path.write_bytes(
                request.experiment_input.canonical_bytes
            )
            valid = self._cli_arguments(
                request, plan_path, repositories
            )
            prefix = valid[:9]
            bindings = valid[9:]
            pairs = [
                tuple(bindings[index : index + 2])
                for index in range(0, len(bindings), 2)
            ]
            private_path = str(next(iter(repositories.values())))
            cases = {
                "missing": prefix
                + [item for pair in pairs[:3] for item in pair],
                "extra": valid
                + ["--task-source", "extra-task=" + private_path],
                "duplicate": valid
                + ["--task-source", "low-alpha=" + private_path],
                "relative": prefix
                + [
                    item
                    for pair in (
                        pairs[:3]
                        + [
                            (
                                "--task-source",
                                "medium-beta=relative/repository",
                            )
                        ]
                    )
                    for item in pair
                ],
                "malformed": prefix
                + [
                    item
                    for pair in (
                        pairs[:3]
                        + [("--task-source", "medium-beta")]
                    )
                    for item in pair
                ],
                "bad_id": prefix
                + [
                    item
                    for pair in (
                        pairs[:3]
                        + [
                            (
                                "--task-source",
                                "bad/id=" + private_path,
                            )
                        ]
                    )
                    for item in pair
                ],
                "unknown": prefix
                + [
                    item
                    for pair in (
                        pairs[:3]
                        + [
                            (
                                "--task-source",
                                "unknown-task=" + private_path,
                            )
                        ]
                    )
                    for item in pair
                ],
            }
            for label, arguments in cases.items():
                with self.subTest(label=label), mock.patch.object(
                    experiment_cli,
                    "run_experiment_preflight",
                ) as runner:
                    return_code, stdout, stderr = self._run_cli(
                        arguments
                    )
                    self.assertEqual(return_code, 2)
                    self.assertEqual(stderr, "")
                    self._assert_fixed_blocked_cli_output(stdout)
                    self.assertEqual(runner.call_count, 0)
                    self.assertNotIn(private_path, stdout + stderr)

    def test_cli_rejects_symlink_hardlink_and_noncanonical_input(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, repositories = (
                self._make_request_fixture(base)
            )
            plan_path = base / "plan.json"
            plan_path.write_bytes(
                request.experiment_input.canonical_bytes
            )
            symlink_path = base / "plan-link.json"
            symlink_path.symlink_to(plan_path)
            hardlink_path = base / "plan-hardlink.json"
            os.link(plan_path, hardlink_path)
            noncanonical_path = base / "noncanonical.json"
            noncanonical_path.write_text(
                json.dumps(
                    json.loads(
                        request.experiment_input.canonical_bytes
                    ),
                    indent=2,
                )
            )
            for candidate in (
                symlink_path,
                hardlink_path,
                noncanonical_path,
            ):
                with self.subTest(candidate=candidate), mock.patch.object(
                    experiment_cli,
                    "run_experiment_preflight",
                ) as runner:
                    arguments = self._cli_arguments(
                        request, candidate, repositories
                    )
                    return_code, stdout, stderr = self._run_cli(
                        arguments
                    )
                    self.assertEqual(return_code, 2)
                    self.assertEqual(stderr, "")
                    self._assert_fixed_blocked_cli_output(stdout)
                    self.assertEqual(runner.call_count, 0)
                    self.assertNotIn(str(candidate), stdout + stderr)

    def test_cli_reader_uses_bounded_nofollow_nonblocking_descriptor(self):
        self.assertEqual(experiment_cli._MAX_INPUT_BYTES, 1024 * 1024)
        self.assertEqual(experiment_cli._READ_CHUNK_BYTES, 64 * 1024)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / "input.json"
            path.write_bytes(b"{}")
            real_open = os.open
            seen_flags = []

            def capture_open(value, flags):
                seen_flags.append(flags)
                return real_open(value, flags)

            with mock.patch.object(
                experiment_cli.os, "open", side_effect=capture_open
            ):
                self.assertEqual(
                    experiment_cli._read_stable_input_file(path), b"{}"
                )
            self.assertEqual(len(seen_flags), 1)
            self.assertTrue(
                seen_flags[0] & getattr(os, "O_NOFOLLOW", 0)
            )
            self.assertTrue(
                seen_flags[0] & getattr(os, "O_NONBLOCK", 0)
            )
            self.assertTrue(
                seen_flags[0] & getattr(os, "O_CLOEXEC", 0)
            )

    def test_cli_validates_all_task_ids_before_any_source_path(self):
        bindings = [
            "low-alpha=relative/first",
            "low-beta=/private/second",
            "medium-alpha=/private/third",
            "bad/id=/private/fourth",
        ]
        with mock.patch.object(
            experiment_cli.os.path,
            "isabs",
            side_effect=AssertionError("path validation ran"),
        ), self.assertRaisesRegex(
            experiment_cli._CLIError,
            "^invalid_input$",
        ):
            experiment_cli._parse_task_source_bindings(bindings)

    def test_cli_reader_closes_descriptor_and_preserves_baseexception(self):
        metadata = os.stat_result(
            (stat.S_IFREG | 0o600, 123, 456, 1, 501, 20, 2, 0, 0, 0)
        )
        abort = _Abort("reader abort")
        with mock.patch.object(
            experiment_cli.os, "lstat", return_value=metadata
        ), mock.patch.object(
            experiment_cli.os, "open", return_value=37
        ), mock.patch.object(
            experiment_cli.os, "fstat", return_value=metadata
        ), mock.patch.object(
            experiment_cli.os, "read", side_effect=abort
        ), mock.patch.object(
            experiment_cli.os, "close"
        ) as close:
            with self.assertRaises(_Abort) as caught:
                experiment_cli._read_stable_input_file(
                    Path("/private/input.json")
                )
        self.assertIs(caught.exception, abort)
        close.assert_called_once_with(37)

    def test_cli_reader_enforces_chunk_size_cap_eof_and_stable_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            path = base / "bounded-input.json"
            payload = b"x" * (
                experiment_cli._READ_CHUNK_BYTES * 2 + 3
            )
            path.write_bytes(payload)
            real_read = os.read
            observations = []

            def capture_read(descriptor, maximum):
                chunk = real_read(descriptor, maximum)
                observations.append((maximum, len(chunk)))
                return chunk

            with mock.patch.object(
                experiment_cli.os,
                "read",
                side_effect=capture_read,
            ):
                self.assertEqual(
                    experiment_cli._read_stable_input_file(path),
                    payload,
                )
            self.assertTrue(observations)
            self.assertTrue(
                all(
                    0 < maximum <= experiment_cli._READ_CHUNK_BYTES
                    for maximum, unused_size in observations
                )
            )
            self.assertEqual(observations[-1][1], 0)
            self.assertEqual(
                sum(size for unused_maximum, size in observations),
                len(payload),
            )

            maximum_path = base / "maximum-input.json"
            maximum_path.write_bytes(
                b"x" * experiment_cli._MAX_INPUT_BYTES
            )
            self.assertEqual(
                len(
                    experiment_cli._read_stable_input_file(
                        maximum_path
                    )
                ),
                experiment_cli._MAX_INPUT_BYTES,
            )
            oversized_path = base / "oversized-input.json"
            oversized_path.write_bytes(
                b"x" * (experiment_cli._MAX_INPUT_BYTES + 1)
            )
            with self.assertRaisesRegex(
                experiment_cli._CLIError,
                "^invalid_input$",
            ):
                experiment_cli._read_stable_input_file(oversized_path)

            mutable_path = base / "mutable-input.json"
            mutable_path.write_bytes(b"{}")
            real_fstat = os.fstat
            fstat_calls = 0

            def mutate_final_fstat(descriptor):
                nonlocal fstat_calls
                fstat_calls += 1
                metadata = real_fstat(descriptor)
                if fstat_calls == 2:
                    values = list(metadata)
                    values[6] += 1
                    return os.stat_result(values)
                return metadata

            with mock.patch.object(
                experiment_cli.os,
                "fstat",
                side_effect=mutate_final_fstat,
            ), self.assertRaisesRegex(
                experiment_cli._CLIError,
                "^invalid_input$",
            ):
                experiment_cli._read_stable_input_file(mutable_path)

    def test_cli_does_not_convert_baseexceptions_to_json(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, repositories = (
                self._make_request_fixture(base)
            )
            plan_path = base / "plan.json"
            plan_path.write_bytes(
                request.experiment_input.canonical_bytes
            )
            arguments = self._cli_arguments(
                request, plan_path, repositories
            )
            for abort in (
                KeyboardInterrupt(),
                SystemExit(73),
                GeneratorExit(),
                _Abort("orchestration abort"),
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with self.subTest(abort=type(abort).__name__), mock.patch.object(
                    experiment_cli,
                    "run_experiment_preflight",
                    side_effect=abort,
                ), redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(type(abort)) as caught:
                        experiment_cli.main(arguments)
                self.assertIs(caught.exception, abort)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    def test_cli_expected_orchestration_exception_is_cleanup_required(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, repositories = (
                self._make_request_fixture(base)
            )
            plan_path = base / "plan.json"
            plan_path.write_bytes(
                request.experiment_input.canonical_bytes
            )
            arguments = self._cli_arguments(
                request, plan_path, repositories
            )
            with mock.patch.object(
                experiment_cli,
                "run_experiment_preflight",
                side_effect=ExperimentPreflightError(
                    "experiment_preflight_invalid"
                ),
            ):
                return_code, stdout, stderr = self._run_cli(arguments)
            self.assertEqual(return_code, 2)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["cleanup_state"], "cleanup_required")
            self.assertEqual(
                payload["reason_code"],
                "task_snapshot_cleanup_required",
            )
            self.assertTrue(
                all(
                    payload[name] is None
                    for name in (
                        "bundle_digest",
                        "current_profile_digest",
                        "lean_profile_digest",
                        "task_corpus_receipt_digest",
                        "plan_digest",
                        "preflight_receipt_digest",
                    )
                )
            )

    def test_zero_call_full_success_is_path_free_and_removes_only_phase_tree(self):
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "harness_experiment"
            / "valid-plan-input.json"
        )
        document = json.loads(fixture.read_text())
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve() / "LOCAL-SENTINEL-PATH"
            base.mkdir(mode=0o700)
            os.chmod(base, 0o700)
            bundle = base / "bundle"
            skill_repo = base / "skill-repo"
            temp_parent = base / "temp-parent"
            second_temp_parent = base / "second-temp-parent"
            temp_parent.mkdir(mode=0o700)
            second_temp_parent.mkdir(mode=0o700)
            os.chmod(temp_parent, 0o700)
            os.chmod(second_temp_parent, 0o700)
            self._make_bundle(bundle)
            self._make_skill_repo(skill_repo)
            repositories = {}
            for candidate in document["candidates"]:
                repo = base / ("source-" + candidate["task_id"])
                candidate["commit_oid"] = self._make_task_repo(
                    repo, candidate["allowed_write_paths"]
                )
                repositories[candidate["task_id"]] = repo
            experiment_input = load_experiment_input(
                canonical_bytes(document)
            )
            sources = {
                candidate["task_id"]: TaskSourceSpec(
                    input_digest=experiment_input.input_digest,
                    task_id=candidate["task_id"],
                    repository_root=repositories[candidate["task_id"]],
                    commit_oid=candidate["commit_oid"],
                    provisioning_class=candidate[
                        "source_provisioning_class"
                    ],
                    operator_attested=candidate[
                        "operator_attested"
                    ],
                    local_clone_policy=candidate[
                        "local_clone_policy"
                    ],
                )
                for candidate in document["candidates"]
            }
            request = ExperimentPreflightRequest(
                experiment_input=experiment_input,
                bundle_root=bundle,
                skill_repo=skill_repo,
                temp_parent=temp_parent,
                task_sources=sources,
            )
            second_request = ExperimentPreflightRequest(
                experiment_input=experiment_input,
                bundle_root=bundle,
                skill_repo=skill_repo,
                temp_parent=second_temp_parent,
                task_sources=sources,
            )
            captured_plans = []
            real_builder = experiment_module.build_experiment_plan

            def capture_plan(*arguments, **keywords):
                plan = real_builder(*arguments, **keywords)
                captured_plans.append(plan)
                return plan

            with mock.patch(
                "scripts.live_eval.experiment.build_experiment_plan",
                side_effect=capture_plan,
            ):
                result = run_experiment_preflight(request)
                reused_result = run_experiment_preflight(request)
                relocated_result = run_experiment_preflight(
                    second_request
                )
            self.assertEqual(result, reused_result)
            self.assertEqual(result, relocated_result)
            self.assertEqual(
                result.status, "static_only", msg=repr(result)
            )
            self.assertEqual(result.model_calls, 0)
            self.assertEqual(result.cleanup_state, "removed")
            self.assertEqual(
                result.reason_code, "static_preflight_verified"
            )
            self.assertFalse((temp_parent / "phase-a").exists())
            self.assertEqual(tuple(temp_parent.iterdir()), ())
            self.assertEqual(tuple(second_temp_parent.iterdir()), ())
            self.assertNotIn(str(base), repr(result))
            for plan in captured_plans:
                self.assertNotIn(
                    str(base).encode("utf-8"), plan.canonical_bytes
                )
                self.assertNotIn(
                    b"LOCAL-SENTINEL", plan.canonical_bytes
                )

    def test_gate_close_abort_is_not_swallowed_by_ordinary_failure(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_close = experiment_module._PhysicalDirectoryGate.close

            def close_then_abort(gate):
                real_close(gate)
                raise _Abort("gate close abort")

            with mock.patch(
                "scripts.live_eval.experiment."
                "_require_physical_disjointness",
                side_effect=RuntimeError("ordinary preflight fault"),
            ), mock.patch.object(
                experiment_module._PhysicalDirectoryGate,
                "close",
                autospec=True,
                side_effect=close_then_abort,
            ):
                with self.assertRaisesRegex(
                    _Abort, "^gate close abort$"
                ):
                    run_experiment_preflight(request)

    def test_phase_file_replacement_is_cleanup_required(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )

            def replace_phase_then_fail(unused_gate):
                (request.temp_parent / "phase-a").write_text(
                    "replacement"
                )
                raise RuntimeError("phase creation fault")

            with mock.patch(
                "scripts.live_eval.experiment._create_phase_ledger",
                side_effect=replace_phase_then_fail,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "cleanup_required")
            self.assertEqual(
                result.reason_code, "task_snapshot_cleanup_required"
            )
            self.assertTrue(
                (request.temp_parent / "phase-a").is_file()
            )

    def test_post_cleanup_receipt_failure_has_removed_mask_without_plan(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_cleanup = experiment_module._cleanup_owned_phase
            with mock.patch(
                "scripts.live_eval.experiment."
                "_success_preflight_receipt",
                side_effect=RuntimeError("receipt fault"),
            ), mock.patch(
                "scripts.live_eval.experiment._cleanup_owned_phase",
                side_effect=real_cleanup,
            ) as cleanup:
                result = run_experiment_preflight(request)
            self.assertEqual(cleanup.call_count, 1)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "removed")
            self.assertEqual(
                result.reason_code, "static_receipt_preflight_blocked"
            )
            self.assertIsNotNone(result.bundle_digest)
            self.assertIsNotNone(result.current_profile_digest)
            self.assertIsNotNone(result.lean_profile_digest)
            self.assertIsNotNone(result.task_corpus_receipt_digest)
            self.assertIsNone(result.plan_digest)
            self.assertIsNone(result.preflight_receipt_digest)
            self.assertFalse(
                (request.temp_parent / "phase-a").exists()
            )

    def test_post_cleanup_success_result_fault_uses_receipt_blocked_mask(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_validate = experiment_module._validate_preflight_result

            def fail_success_result(result):
                if result.status == "static_only":
                    raise RuntimeError("success result fault")
                return real_validate(result)

            with mock.patch(
                "scripts.live_eval.experiment."
                "_validate_preflight_result",
                side_effect=fail_success_result,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "removed")
            self.assertEqual(
                result.reason_code, "static_receipt_preflight_blocked"
            )
            self.assertIsNotNone(result.task_corpus_receipt_digest)
            self.assertIsNone(result.plan_digest)
            self.assertIsNone(result.preflight_receipt_digest)
            self.assertFalse(
                (request.temp_parent / "phase-a").exists()
            )

    def test_task_materializer_operations_have_exact_call_counts(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, document, unused_repositories = (
                self._make_request_fixture(base)
            )
            materializer_type = (
                experiment_module.TaskSnapshotMaterializer
            )
            real_prepare = experiment_module.prepare_task_source
            real_capture = materializer_type.capture
            real_materialize_pair = materializer_type.materialize_pair
            real_verify = materializer_type.verify
            real_close = materializer_type.close

            with mock.patch(
                "scripts.live_eval.experiment.prepare_task_source",
                side_effect=real_prepare,
            ) as prepare, mock.patch.object(
                materializer_type,
                "capture",
                autospec=True,
                side_effect=real_capture,
            ) as capture, mock.patch.object(
                materializer_type,
                "materialize_pair",
                autospec=True,
                side_effect=real_materialize_pair,
            ) as materialize_pair, mock.patch.object(
                materializer_type,
                "verify",
                autospec=True,
                side_effect=real_verify,
            ) as verify, mock.patch.object(
                materializer_type,
                "close",
                autospec=True,
                side_effect=real_close,
            ) as close:
                result = run_experiment_preflight(request)

            task_count = len(document["candidates"])
            self.assertEqual(result.status, "static_only")
            self.assertEqual(prepare.call_count, task_count)
            self.assertEqual(capture.call_count, task_count)
            self.assertEqual(materialize_pair.call_count, task_count)
            self.assertEqual(verify.call_count, 0)
            self.assertEqual(close.call_count, task_count)

    def test_source_mutation_between_prepare_and_capture_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, repositories = (
                self._make_request_fixture(base)
            )
            real_capture = (
                experiment_module.TaskSnapshotMaterializer.capture
            )
            mutated = {"value": False}

            def mutate_then_capture(materializer, prepared):
                if not mutated["value"]:
                    mutated["value"] = True
                    target = repositories["low-alpha"] / ".git" / "config"
                    target.write_text(
                        target.read_text() + "\n# capture mutation\n"
                    )
                return real_capture(materializer, prepared)

            with mock.patch.object(
                experiment_module.TaskSnapshotMaterializer,
                "capture",
                autospec=True,
                side_effect=mutate_then_capture,
            ):
                result = run_experiment_preflight(request)
            self.assertTrue(mutated["value"])
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "removed")
            self.assertEqual(
                result.reason_code, "task_snapshot_preflight_blocked"
            )
            self.assertIsNone(result.task_corpus_receipt_digest)
            self.assertIsNone(result.plan_digest)
            self.assertFalse(
                (request.temp_parent / "phase-a").exists()
            )

    def test_late_bundle_home_and_task_mutations_block(self):
        real_recheck = experiment_module._final_mutation_recheck
        for category in ("bundle", "home", "task"):
            with self.subTest(category=category):
                with tempfile.TemporaryDirectory() as root:
                    base = Path(root).resolve()
                    request, unused_document, unused_repositories = (
                        self._make_request_fixture(base)
                    )

                    def mutate_then_recheck(**keywords):
                        if category == "bundle":
                            target = (
                                request.bundle_root
                                / "profiles"
                                / "current"
                                / "AGENTS.md"
                            )
                        elif category == "home":
                            target = (
                                request.temp_parent
                                / "phase-a"
                                / "homes"
                                / "current"
                                / "AGENTS.md"
                            )
                        else:
                            target = (
                                request.temp_parent
                                / "phase-a"
                                / "tasks"
                                / "low-alpha"
                                / "current"
                                / "scripts"
                                / "alpha.py"
                            )
                        os.chmod(target, 0o600)
                        target.write_text("late mutation\n")
                        return real_recheck(**keywords)

                    with mock.patch(
                        "scripts.live_eval.experiment."
                        "_final_mutation_recheck",
                        side_effect=mutate_then_recheck,
                    ):
                        result = run_experiment_preflight(request)
                    self.assertEqual(result.status, "blocked")
                    self.assertEqual(result.model_calls, 0)
                    self.assertIsNone(result.plan_digest)
                    self.assertIsNone(result.preflight_receipt_digest)
                    self.assertIsNotNone(
                        result.task_corpus_receipt_digest
                    )
                    self.assertEqual(
                        result.reason_code,
                        (
                            "task_snapshot_cleanup_required"
                            if category in ("home", "task")
                            else "harness_preflight_blocked"
                        ),
                    )
                    if category in ("home", "task"):
                        self.assertEqual(
                            result.cleanup_state, "cleanup_required"
                        )
                        self.assertTrue(
                            (request.temp_parent / "phase-a").exists()
                        )
                    else:
                        self.assertEqual(
                            result.cleanup_state, "removed"
                        )
                        self.assertFalse(
                            (request.temp_parent / "phase-a").exists()
                        )
                    self.assertNotIn(str(base), repr(result))
                    self.assertNotIn("late mutation", repr(result))

    def test_ordinary_partial_handoff_quarantines_and_abort_propagates(self):
        class Abort(BaseException):
            pass

        for exception_type in (RuntimeError, Abort):
            with self.subTest(exception_type=exception_type.__name__):
                with tempfile.TemporaryDirectory() as root:
                    base = Path(root).resolve()
                    request, unused_document, unused_repositories = (
                        self._make_request_fixture(base)
                    )

                    def partial_home(
                        unused_skill,
                        unused_bundle,
                        unused_profile,
                        codex_home,
                    ):
                        partial = Path(codex_home) / "partial"
                        partial.write_text(
                            "LOCAL-SENTINEL-PARTIAL-HANDOFF"
                        )
                        raise exception_type(
                            "downstream detail {}".format(base)
                        )

                    with mock.patch(
                        "scripts.live_eval.experiment."
                        "materialize_harness_home",
                        side_effect=partial_home,
                    ):
                        if exception_type is Abort:
                            with self.assertRaises(Abort):
                                run_experiment_preflight(request)
                        else:
                            result = run_experiment_preflight(request)
                            self.assertEqual(result.status, "blocked")
                            self.assertEqual(result.model_calls, 0)
                            self.assertEqual(
                                result.cleanup_state,
                                "cleanup_required",
                            )
                            self.assertEqual(
                                result.reason_code,
                                "task_snapshot_cleanup_required",
                            )
                            self.assertIsNone(
                                result.preflight_receipt_digest
                            )
                            self.assertNotIn(str(base), repr(result))
                            self.assertNotIn(
                                "downstream detail", repr(result)
                            )
                    self.assertTrue(
                        (
                            request.temp_parent
                            / "phase-a"
                            / "homes"
                            / "current"
                            / "partial"
                        ).is_file()
                    )

    def test_returned_pair_validation_failure_quarantines(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            with mock.patch(
                "scripts.live_eval.experiment."
                "_require_materialized_pair",
                side_effect=ExperimentPreflightError(
                    "task_snapshot_preflight_blocked"
                ),
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "cleanup_required")
            self.assertEqual(
                result.reason_code, "task_snapshot_cleanup_required"
            )
            self.assertIsNone(result.preflight_receipt_digest)
            self.assertTrue(
                (request.temp_parent / "phase-a" / "tasks").is_dir()
            )

    def test_returned_pair_noncanonical_receipt_fails_before_acquisition(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_materialize_pair = (
                experiment_module.TaskSnapshotMaterializer.materialize_pair
            )

            def materialize_then_forge_receipt(
                materializer,
                captured,
                target_parent,
                current_name="current",
                lean_name="lean",
            ):
                pair = real_materialize_pair(
                    materializer,
                    captured,
                    target_parent,
                    current_name,
                    lean_name,
                )
                forged = _AlwaysEqualValue(
                    pair[0].snapshot_receipt.payload
                )
                object.__setattr__(
                    pair[0],
                    "snapshot_receipt",
                    forged,
                )
                object.__setattr__(
                    pair[1],
                    "snapshot_receipt",
                    forged,
                )
                return pair

            with mock.patch.object(
                experiment_module.TaskSnapshotMaterializer,
                "materialize_pair",
                autospec=True,
                side_effect=materialize_then_forge_receipt,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "cleanup_required")
            self.assertEqual(
                result.reason_code, "task_snapshot_cleanup_required"
            )
            self.assertTrue(
                (request.temp_parent / "phase-a" / "tasks").is_dir()
            )

    def test_returned_pair_noninteger_count_cannot_compare_equal(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_materialize_pair = (
                experiment_module.TaskSnapshotMaterializer.materialize_pair
            )

            def materialize_then_forge_count(
                materializer,
                captured,
                target_parent,
                current_name="current",
                lean_name="lean",
            ):
                pair = real_materialize_pair(
                    materializer,
                    captured,
                    target_parent,
                    current_name,
                    lean_name,
                )
                object.__setattr__(
                    pair[0],
                    "file_count",
                    _AlwaysEqualValue(),
                )
                object.__setattr__(
                    pair[1],
                    "file_count",
                    _AlwaysEqualValue(),
                )
                return pair

            with mock.patch.object(
                experiment_module.TaskSnapshotMaterializer,
                "materialize_pair",
                autospec=True,
                side_effect=materialize_then_forge_count,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "cleanup_required")
            self.assertEqual(
                result.reason_code, "task_snapshot_cleanup_required"
            )
            self.assertTrue(
                (request.temp_parent / "phase-a" / "tasks").is_dir()
            )

    def test_returned_pair_post_return_extra_file_is_not_acquired(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_materialize_pair = (
                experiment_module.TaskSnapshotMaterializer.materialize_pair
            )

            def materialize_then_add_extra(
                materializer,
                captured,
                target_parent,
                current_name="current",
                lean_name="lean",
            ):
                pair = real_materialize_pair(
                    materializer,
                    captured,
                    target_parent,
                    current_name,
                    lean_name,
                )
                os.chmod(pair[0].target_root, 0o700)
                extra = pair[0].target_root / "post-return-extra"
                extra.write_text("must not be acquired")
                return pair

            with mock.patch.object(
                experiment_module.TaskSnapshotMaterializer,
                "materialize_pair",
                autospec=True,
                side_effect=materialize_then_add_extra,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "cleanup_required")
            self.assertEqual(
                result.reason_code, "task_snapshot_cleanup_required"
            )
            self.assertIsNone(result.preflight_receipt_digest)
            self.assertTrue(
                (
                    request.temp_parent
                    / "phase-a"
                    / "tasks"
                    / "low-alpha"
                    / "current"
                    / "post-return-extra"
                ).is_file()
            )

    def test_returned_pair_post_return_content_mutation_is_not_acquired(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_materialize_pair = (
                experiment_module.TaskSnapshotMaterializer.materialize_pair
            )

            def materialize_then_mutate_content(
                materializer,
                captured,
                target_parent,
                current_name="current",
                lean_name="lean",
            ):
                pair = real_materialize_pair(
                    materializer,
                    captured,
                    target_parent,
                    current_name,
                    lean_name,
                )
                target = (
                    pair[0].target_root / "scripts" / "alpha.py"
                )
                if target.is_file():
                    original = target.read_bytes()
                    original_mode = stat.S_IMODE(target.stat().st_mode)
                    mutated = bytes([original[0] ^ 1]) + original[1:]
                    os.chmod(target, 0o600)
                    target.write_bytes(mutated)
                    os.chmod(target, original_mode)
                return pair

            with mock.patch.object(
                experiment_module.TaskSnapshotMaterializer,
                "materialize_pair",
                autospec=True,
                side_effect=materialize_then_mutate_content,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "cleanup_required")
            self.assertEqual(
                result.reason_code, "task_snapshot_cleanup_required"
            )
            self.assertIsNone(result.preflight_receipt_digest)
            self.assertTrue(
                (
                    request.temp_parent
                    / "phase-a"
                    / "tasks"
                    / "low-alpha"
                    / "current"
                    / "scripts"
                    / "alpha.py"
                ).is_file()
            )

    def test_returned_pair_same_content_inode_replacement_is_not_acquired(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_materialize_pair = (
                experiment_module.TaskSnapshotMaterializer.materialize_pair
            )

            def materialize_then_replace_inode(
                materializer,
                captured,
                target_parent,
                current_name="current",
                lean_name="lean",
            ):
                pair = real_materialize_pair(
                    materializer,
                    captured,
                    target_parent,
                    current_name,
                    lean_name,
                )
                target = (
                    pair[0].target_root / "scripts" / "alpha.py"
                )
                if target.is_file():
                    content = target.read_bytes()
                    mode = stat.S_IMODE(target.stat().st_mode)
                    parent = target.parent
                    os.chmod(parent, 0o755)
                    target.unlink()
                    target.write_bytes(content)
                    os.chmod(target, mode)
                    os.chmod(parent, 0o555)
                return pair

            with mock.patch.object(
                experiment_module.TaskSnapshotMaterializer,
                "materialize_pair",
                autospec=True,
                side_effect=materialize_then_replace_inode,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "cleanup_required")
            self.assertEqual(
                result.reason_code, "task_snapshot_cleanup_required"
            )
            self.assertIsNone(result.preflight_receipt_digest)
            self.assertTrue(
                (
                    request.temp_parent
                    / "phase-a"
                    / "tasks"
                    / "low-alpha"
                    / "current"
                    / "scripts"
                    / "alpha.py"
                ).is_file()
            )

    def test_returned_home_verification_failure_quarantines(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            blocked = experiment_module.HarnessPreflightResult(
                classification="blocked_isolation",
                result="blocked",
                reason="home_seal_mismatch",
            )
            with mock.patch(
                "scripts.live_eval.experiment.verify_loaded_harness",
                return_value=blocked,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "cleanup_required")
            self.assertEqual(
                result.reason_code, "task_snapshot_cleanup_required"
            )
            self.assertIsNone(result.preflight_receipt_digest)
            self.assertTrue(
                (
                    request.temp_parent
                    / "phase-a"
                    / "homes"
                    / "current"
                    / "AGENTS.md"
                ).is_file()
            )

    def test_post_plan_cleanup_failure_retains_five_digests_without_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            with mock.patch(
                "scripts.live_eval.experiment._cleanup_owned_phase",
                return_value=False,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "cleanup_required")
            self.assertEqual(
                result.reason_code, "task_snapshot_cleanup_required"
            )
            self.assertIsNotNone(result.bundle_digest)
            self.assertIsNotNone(result.current_profile_digest)
            self.assertIsNotNone(result.lean_profile_digest)
            self.assertIsNotNone(result.task_corpus_receipt_digest)
            self.assertIsNotNone(result.plan_digest)
            self.assertIsNone(result.preflight_receipt_digest)
            self.assertEqual(result.model_calls, 0)
            self.assertTrue(
                (request.temp_parent / "phase-a").is_dir()
            )

    def test_path_leaking_plan_is_blocked_before_plan_digest_publication(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_build_plan = experiment_module._build_plan

            def build_leaking_plan(*arguments, **keywords):
                plan = real_build_plan(*arguments, **keywords)
                return replace(
                    plan,
                    canonical_bytes=(
                        plan.canonical_bytes
                        + str(request.temp_parent).encode("utf-8")
                    ),
                )

            with mock.patch(
                "scripts.live_eval.experiment._build_plan",
                side_effect=build_leaking_plan,
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.cleanup_state, "removed")
            self.assertEqual(
                result.reason_code,
                "experiment_plan_preflight_blocked",
            )
            self.assertIsNotNone(result.task_corpus_receipt_digest)
            self.assertIsNone(result.plan_digest)
            self.assertIsNone(result.preflight_receipt_digest)
            self.assertFalse(
                (request.temp_parent / "phase-a").exists()
            )
            self.assertNotIn(str(base), repr(result))

    def test_poison_pills_allow_only_git_and_no_network_resolution_or_recursive_cleanup(self):
        source = inspect.getsource(experiment_module)
        parsed = ast.parse(source)
        forbidden_imports = {
            "requests",
            "httpx",
            "shutil",
            "socket",
            "subprocess",
            "tempfile",
            "urllib",
        }
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(parsed)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(imported.isdisjoint(forbidden_imports))
        forbidden_attributes = {
            "TemporaryDirectory",
            "getenv",
            "rglob",
            "rmtree",
            "walk",
            "which",
        }
        self.assertFalse(
            any(
                isinstance(node, ast.Attribute)
                and node.attr in forbidden_attributes
                for node in ast.walk(parsed)
            )
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Attribute)
                and node.attr == "environ"
                for node in ast.walk(parsed)
            )
        )

        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve()
            request, unused_document, unused_repositories = (
                self._make_request_fixture(base)
            )
            real_popen = subprocess.Popen
            poison_attempts = []

            def git_only_popen(arguments, *args, **kwargs):
                executable = (
                    arguments[0]
                    if isinstance(arguments, (tuple, list))
                    else arguments
                )
                if Path(os.fspath(executable)).name != "git":
                    poison_attempts.append(
                        "process:" + os.fspath(executable)
                    )
                    raise AssertionError("non-Git process attempted")
                return real_popen(arguments, *args, **kwargs)

            git_only_popen.__signature__ = inspect.signature(real_popen)

            def poison(name):
                def reject(*unused_args, **unused_kwargs):
                    poison_attempts.append(name)
                    raise AssertionError(name + " attempted")

                return reject

            with mock.patch(
                "subprocess.Popen", new=git_only_popen
            ), mock.patch(
                "socket.create_connection",
                side_effect=poison("network"),
            ), mock.patch(
                "urllib.request.urlopen",
                side_effect=poison("HTTP"),
            ), mock.patch(
                "shutil.which",
                side_effect=poison("resolution"),
            ), mock.patch(
                "tempfile.TemporaryDirectory",
                side_effect=poison("recursive temp"),
            ):
                result = run_experiment_preflight(request)
            self.assertEqual(
                result.status,
                "static_only",
                msg="{} attempts={!r}".format(
                    repr(result), poison_attempts
                ),
            )
            self.assertEqual(result.model_calls, 0)


if __name__ == "__main__":
    unittest.main()

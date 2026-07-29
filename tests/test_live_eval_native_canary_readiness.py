"""Tests for native canary readiness observations."""

import dataclasses
import copy
import errno
import hashlib
import inspect
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import scripts.live_eval.native_canary_readiness as readiness_module
from scripts.live_eval.private_jsonl_ledger import PrivateJSONLLedgerError
from scripts.live_eval.native_canary_readiness import (
    BoundedCommand,
    BoundedCommandResult,
    CHILD_SOURCE,
    CONFIG_BYTES,
    PERMISSION_PROFILE_NAME,
    NativeCanaryReadinessRequest,
    NativeCanaryReadinessResult,
    _PRODUCTION_POLICY,
    _NativeReadinessPolicy,
    _EvidencePublisher,
    _LoopbackListener,
    _ResourceRegistry,
    _SealedExecutable,
    _TrustedProbeDirectory,
    _parse_child,
    _run_bounded_command,
    _run_with_policy,
    _wait_for_lock_probe,
)
from scripts.workflow_coordination.canonical_json import canonical_bytes, sha256_id


class NativeCanaryReadinessContractTests(unittest.TestCase):
    @staticmethod
    def _valid_child_payload():
        digest = "a" * 64
        return {
            "allowed_read": {
                "digest": digest,
                "errno": None,
                "ok": True,
                "overflow": False,
            },
            "allowed_write": {"errno": None, "ok": True},
            "environment": {"digest": digest, "present": True},
            "forbidden_read": {
                "digest": None,
                "errno": errno.EACCES,
                "ok": False,
                "overflow": False,
            },
            "forbidden_write": {"errno": errno.EPERM, "ok": False},
            "network": {"errno": errno.EPERM, "ok": False},
            "schema_version": 1,
        }

    @staticmethod
    def _child_result(payload):
        return BoundedCommandResult(
            0,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            + b"\n",
            b"",
            False,
            False,
        )

    def test_child_schema_rejects_bool_integer_aliases_and_bad_scalars(self) -> None:
        mutations = (
            (("schema_version",), True),
            (("allowed_read", "digest"), 7),
            (("allowed_read", "digest"), "not-a-digest"),
            (("allowed_read", "errno"), True),
            (("allowed_read", "ok"), 1),
            (("allowed_read", "overflow"), 0),
            (("allowed_write", "errno"), False),
            (("allowed_write", "ok"), 1),
            (("environment", "digest"), False),
            (("environment", "present"), 0),
            (("forbidden_read", "errno"), True),
            (("forbidden_read", "ok"), 0),
            (("forbidden_read", "overflow"), 1),
            (("forbidden_write", "errno"), True),
            (("forbidden_write", "ok"), 0),
            (("network", "errno"), True),
            (("network", "ok"), 0),
        )
        for path, replacement in mutations:
            payload = self._valid_child_payload()
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            with self.subTest(path=path, replacement=replacement):
                with self.assertRaises(RuntimeError):
                    _parse_child(self._child_result(payload))

    def test_child_schema_rejects_malformed_json(self) -> None:
        with self.assertRaises(RuntimeError):
            _parse_child(
                BoundedCommandResult(0, b"{\n", b"", False, False)
            )

    def test_output_token_rejects_replacement_before_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory_path = Path(raw).resolve() / "probe"
            directory_path.mkdir(mode=0o700)
            directory = _TrustedProbeDirectory(directory_path)
            output_path = directory_path / "output"
            output_path.write_bytes(b"token")
            output_path.chmod(0o600)
            output = directory.open_output("output", b"token")
            original = directory_path / "output-original"
            output_path.rename(original)
            output_path.write_bytes(b"token")
            output_path.chmod(0o600)
            try:
                with self.assertRaises(OSError):
                    output.unlink()
                self.assertTrue(output_path.exists())
            finally:
                output.close()
                directory.close()

    def test_partial_probe_acquisition_closes_prior_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            first_path = base / "first"
            second_path = base / "second"
            first_path.mkdir(mode=0o700)
            second_path.mkdir(mode=0o700)

            registry = _ResourceRegistry()
            first_directory = registry.add(
                _TrustedProbeDirectory(first_path)
            )
            first_fd = first_directory.descriptor
            second_path.chmod(0o755)
            try:
                with self.assertRaises(OSError):
                    _TrustedProbeDirectory(second_path)
            finally:
                registry.close()
                second_path.chmod(0o700)
            with self.assertRaises(OSError):
                os.fstat(first_fd)

            registry = _ResourceRegistry()
            first_directory = registry.add(
                _TrustedProbeDirectory(first_path)
            )
            second_directory = registry.add(
                _TrustedProbeDirectory(second_path)
            )
            first_sentinel = registry.add(
                first_directory.create("sentinel", b"first")
            )
            sentinel_fd = first_sentinel._descriptor
            try:
                with self.assertRaises(FileExistsError):
                    first_directory.create("sentinel", b"duplicate")
            finally:
                registry.close()
            with self.assertRaises(OSError):
                os.fstat(sentinel_fd)

            output_path = first_path / "output"
            output_path.write_bytes(b"token")
            output_path.chmod(0o600)
            registry = _ResourceRegistry()
            first_directory = registry.add(
                _TrustedProbeDirectory(first_path)
            )
            first_output = registry.add(
                first_directory.open_output("output", b"token")
            )
            output_fd = first_output._descriptor
            try:
                with self.assertRaises(FileNotFoundError):
                    first_directory.open_output("missing-output", b"token")
            finally:
                registry.close()
            with self.assertRaises(OSError):
                os.fstat(output_fd)

    def test_resource_registry_does_not_retry_unknown_failed_resource(
        self,
    ) -> None:
        interruption = KeyboardInterrupt("resource close interrupted")

        class Resource:
            def __init__(self, failure=None):
                self.calls = 0
                self.closed = False
                self.failure = failure

            def close(self):
                self.calls += 1
                if self.failure is not None:
                    raise self.failure
                self.closed = True

        first = Resource()
        failed = Resource(interruption)
        last = Resource()
        registry = _ResourceRegistry()
        registry.add(first)
        registry.add(failed)
        registry.add(last)
        with self.assertRaises(KeyboardInterrupt) as raised:
            registry.close()
        self.assertIs(raised.exception, interruption)
        self.assertEqual(failed.calls, 1)
        self.assertFalse(failed.closed)
        self.assertEqual(first.calls, 1)
        self.assertTrue(first.closed)
        self.assertEqual(last.calls, 1)
        self.assertTrue(last.closed)

    def test_success_evidence_post_replace_failures_restore_blocked(self) -> None:
        blocked = {
            "cleanup_state": "removed",
            "reason_code": "evidence_retention_failed",
            "schema_version": 1,
            "status": "blocked",
        }
        success = {
            "cleanup_state": "removed",
            "reason_code": "native_primitive_observations_recorded",
            "schema_version": 1,
            "status": "native_primitive_observations_only",
        }
        for fault in ("fsync", "read", "path_swap"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as raw:
                private_root = Path(raw).resolve() / "private"
                private_root.mkdir(mode=0o700)
                root_fd = os.open(
                    str(private_root),
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                root_info = os.fstat(root_fd)
                publisher = _EvidencePublisher(
                    private_root,
                    root_fd,
                    (root_info.st_dev, root_info.st_ino),
                )
                os.close(root_fd)
                publisher.publish(blocked)
                triggered = {"value": False}
                original_fsync = readiness_module.os.fsync
                original_read = readiness_module.os.read
                original_stat = readiness_module.os.stat

                def failing_fsync(descriptor):
                    if (
                        not triggered["value"]
                        and descriptor == publisher._run_fd
                    ):
                        triggered["value"] = True
                        raise OSError(errno.EIO, "post-rename fsync fault")
                    return original_fsync(descriptor)

                def failing_read(descriptor, limit):
                    if not triggered["value"]:
                        triggered["value"] = True
                        raise OSError(errno.EIO, "post-rename read fault")
                    return original_read(descriptor, limit)

                def swapping_stat(path, *args, **kwargs):
                    if (
                        not triggered["value"]
                        and path == "readiness.json"
                        and kwargs.get("dir_fd") == publisher._run_fd
                    ):
                        triggered["value"] = True
                        publisher.path.rename(
                            publisher.path.with_name("swapped-success")
                        )
                        publisher.path.symlink_to("missing-evidence")
                    return original_stat(path, *args, **kwargs)

                replacement = {
                    "fsync": mock.patch.object(
                        readiness_module.os, "fsync", side_effect=failing_fsync
                    ),
                    "read": mock.patch.object(
                        readiness_module.os, "read", side_effect=failing_read
                    ),
                    "path_swap": mock.patch.object(
                        readiness_module.os, "stat", side_effect=swapping_stat
                    ),
                }[fault]
                try:
                    with replacement, self.assertRaises(OSError):
                        publisher.publish(success, fallback=blocked)
                    self.assertTrue(triggered["value"])
                    self.assertEqual(
                        publisher.path.read_bytes(), canonical_bytes(blocked)
                    )
                    for artifact in publisher.run_directory.iterdir():
                        if artifact.is_file():
                            self.assertNotEqual(
                                artifact.read_bytes(), canonical_bytes(success)
                            )
                finally:
                    publisher.close()

    def test_initial_evidence_post_replace_failure_removes_pending(self) -> None:
        pending = {
            "cleanup_state": "pending",
            "reason_code": "cleanup_required",
            "schema_version": 1,
            "status": "blocked",
        }
        with tempfile.TemporaryDirectory() as raw:
            private_root = Path(raw).resolve() / "private"
            private_root.mkdir(mode=0o700)
            root_fd = os.open(
                str(private_root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            root_info = os.fstat(root_fd)
            publisher = _EvidencePublisher(
                private_root,
                root_fd,
                (root_info.st_dev, root_info.st_ino),
            )
            os.close(root_fd)
            triggered = {"value": False}
            original_fsync = readiness_module.os.fsync

            def failing_fsync(descriptor):
                if not triggered["value"] and descriptor == publisher._run_fd:
                    triggered["value"] = True
                    raise OSError(errno.EIO, "post-rename fsync fault")
                return original_fsync(descriptor)

            try:
                with mock.patch.object(
                    readiness_module.os, "fsync", side_effect=failing_fsync
                ), self.assertRaises(OSError):
                    publisher.publish(pending)
                self.assertTrue(triggered["value"])
                self.assertEqual(list(publisher.run_directory.iterdir()), [])
            finally:
                publisher.close()

    def test_evidence_publisher_constructor_interrupt_removes_owned_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            private_root = Path(raw).resolve() / "private"
            private_root.mkdir(mode=0o700)
            root_fd = os.open(
                str(private_root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            root_info = os.fstat(root_fd)
            interruption = KeyboardInterrupt("constructor interrupted")
            before = len(os.listdir("/dev/fd"))
            try:
                with mock.patch.object(
                    readiness_module.os,
                    "fsync",
                    side_effect=interruption,
                ), self.assertRaises(KeyboardInterrupt) as raised:
                    _EvidencePublisher(
                        private_root,
                        root_fd,
                        (root_info.st_dev, root_info.st_ino),
                    )
                self.assertIs(raised.exception, interruption)
                self.assertEqual(list(private_root.iterdir()), [])
                self.assertEqual(len(os.listdir("/dev/fd")), before)
            finally:
                os.close(root_fd)

    def test_evidence_publisher_constructor_stat_interrupt_removes_empty_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            private_root = Path(raw).resolve() / "private"
            private_root.mkdir(mode=0o700)
            root_fd = os.open(
                str(private_root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            root_info = os.fstat(root_fd)
            interruption = KeyboardInterrupt("identity stat interrupted")
            actual_stat = os.stat

            def interrupt_candidate_stat(path, *args, **kwargs):
                if (
                    isinstance(path, str)
                    and path.startswith("native-readiness-")
                    and kwargs.get("dir_fd") is not None
                ):
                    raise interruption
                return actual_stat(path, *args, **kwargs)

            before = len(os.listdir("/dev/fd"))
            try:
                with mock.patch.object(
                    readiness_module.os,
                    "stat",
                    side_effect=interrupt_candidate_stat,
                ), self.assertRaises(KeyboardInterrupt) as raised:
                    _EvidencePublisher(
                        private_root,
                        root_fd,
                        (root_info.st_dev, root_info.st_ino),
                    )
                self.assertIs(raised.exception, interruption)
                self.assertEqual(list(private_root.iterdir()), [])
                self.assertEqual(len(os.listdir("/dev/fd")), before)
            finally:
                os.close(root_fd)

    def test_pending_publish_interrupt_removes_replaced_identity(self) -> None:
        pending = {
            "cleanup_state": "pending",
            "reason_code": "cleanup_required",
            "schema_version": 1,
            "status": "blocked",
        }
        with tempfile.TemporaryDirectory() as raw:
            private_root = Path(raw).resolve() / "private"
            private_root.mkdir(mode=0o700)
            root_fd = os.open(
                str(private_root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            root_info = os.fstat(root_fd)
            publisher = _EvidencePublisher(
                private_root,
                root_fd,
                (root_info.st_dev, root_info.st_ino),
            )
            os.close(root_fd)
            interruption = KeyboardInterrupt("pending verify interrupted")
            try:
                with mock.patch.object(
                    publisher,
                    "_verify_descriptor",
                    side_effect=interruption,
                ), self.assertRaises(KeyboardInterrupt) as raised:
                    publisher.publish(pending)
                self.assertIs(raised.exception, interruption)
                self.assertEqual(list(publisher.run_directory.iterdir()), [])
            finally:
                publisher.close()

    def test_evidence_publisher_abort_does_not_close_reused_descriptor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            private_root = Path(raw).resolve() / "private"
            private_root.mkdir(mode=0o700)
            root_fd = os.open(
                str(private_root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            root_info = os.fstat(root_fd)
            publisher = _EvidencePublisher(
                private_root,
                root_fd,
                (root_info.st_dev, root_info.st_ino),
            )
            os.close(root_fd)
            run_fd = publisher._run_fd
            publisher_root_fd = publisher._root_fd
            interruption = KeyboardInterrupt("publisher close interrupted")
            actual_close = os.close
            actual_open = os.open
            replacement = []

            def reuse_run_descriptor_then_interrupt(descriptor):
                if descriptor == run_fd and not replacement:
                    actual_close(descriptor)
                    source = actual_open("/dev/null", os.O_RDONLY)
                    if source != descriptor:
                        os.dup2(source, descriptor)
                        actual_close(source)
                    replacement.append(descriptor)
                    raise interruption
                return actual_close(descriptor)

            try:
                with mock.patch.object(
                    readiness_module.os,
                    "close",
                    side_effect=reuse_run_descriptor_then_interrupt,
                ), self.assertRaises(KeyboardInterrupt) as raised:
                    publisher.abort()
                self.assertIs(raised.exception, interruption)
                self.assertEqual(len(replacement), 1)
                os.fstat(replacement[0])
                with self.assertRaises(OSError):
                    os.fstat(publisher_root_fd)
                self.assertEqual(list(private_root.iterdir()), [])
            finally:
                for descriptor in replacement + [run_fd, publisher_root_fd]:
                    try:
                        actual_close(descriptor)
                    except OSError:
                        pass

    def test_fatal_abort_reopens_root_after_publisher_late_close_interrupt(
        self,
    ) -> None:
        terminal = {
            "cleanup_state": "removed",
            "reason_code": "native_primitive_observations_recorded",
            "schema_version": 1,
            "status": "native_primitive_observations_only",
        }

        class ClosedRoots:
            temp_fd = -1

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as raw:
            before_fds = len(os.listdir("/dev/fd"))
            private_root = Path(raw).resolve() / "private"
            private_root.mkdir(mode=0o700)
            root_fd = os.open(
                str(private_root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            root_info = os.fstat(root_fd)
            publisher = _EvidencePublisher(
                private_root,
                root_fd,
                (root_info.st_dev, root_info.st_ino),
            )
            os.close(root_fd)
            publisher.publish(terminal)
            run_fd = publisher._run_fd
            interruption = KeyboardInterrupt("publisher late close interrupted")
            actual_close = os.close
            actual_open = os.open
            replacement = []

            def reuse_run_descriptor_then_interrupt(descriptor):
                if descriptor == run_fd and not replacement:
                    actual_close(descriptor)
                    source = actual_open("/dev/null", os.O_RDONLY)
                    if source != descriptor:
                        os.dup2(source, descriptor)
                        actual_close(source)
                    replacement.append(descriptor)
                    raise interruption
                return actual_close(descriptor)

            try:
                with mock.patch.object(
                    readiness_module.os,
                    "close",
                    side_effect=reuse_run_descriptor_then_interrupt,
                ), self.assertRaises(KeyboardInterrupt) as raised:
                    publisher.close()
                self.assertIs(raised.exception, interruption)
                self.assertEqual(publisher._run_fd, -1)
                self.assertEqual(publisher._root_fd, -1)
                readiness_module._abort_native_readiness_run(
                    publisher, ClosedRoots(), None, None
                )
                os.fstat(replacement[0])
                self.assertEqual(list(private_root.iterdir()), [])
            finally:
                for descriptor in replacement:
                    try:
                        actual_close(descriptor)
                    except OSError:
                        pass
            self.assertEqual(len(os.listdir("/dev/fd")), before_fds)

    def test_publisher_abort_reopen_rejects_replaced_root_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            private_root = base / "private"
            private_root.mkdir(mode=0o700)
            root_fd = os.open(
                str(private_root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            root_info = os.fstat(root_fd)
            publisher = _EvidencePublisher(
                private_root,
                root_fd,
                (root_info.st_dev, root_info.st_ino),
            )
            os.close(root_fd)
            publisher.close()
            original_root = base / "private-original"
            private_root.rename(original_root)
            private_root.mkdir(mode=0o700)
            marker = private_root / "do-not-remove"
            marker.write_bytes(b"replacement")

            with self.assertRaises(OSError) as raised:
                publisher.abort()
            self.assertEqual(raised.exception.errno, errno.EPERM)
            self.assertEqual(
                raised.exception.strerror,
                "evidence root identity changed",
            )
            self.assertEqual(marker.read_bytes(), b"replacement")
            self.assertEqual(
                len(list(original_root.glob("native-readiness-*"))), 1
            )

    def test_fatal_abort_does_not_close_reused_roots_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            temp_parent = base / "temp"
            private_root = base / "private"
            temp_parent.mkdir(mode=0o700)
            private_root.mkdir(mode=0o700)
            executable = Path(os.path.realpath(sys.executable))
            roots = readiness_module._validate_request(
                NativeCanaryReadinessRequest(
                    executable,
                    executable,
                    temp_parent,
                    private_root,
                )
            )
            self.assertIsNotNone(roots)
            private_fd = roots.private_fd
            temp_fd = roots.temp_fd
            interruption = KeyboardInterrupt("roots close interrupted")
            actual_close = os.close
            actual_open = os.open
            replacement = []

            def reuse_private_descriptor_then_interrupt(descriptor):
                if descriptor == private_fd and not replacement:
                    actual_close(descriptor)
                    source = actual_open("/dev/null", os.O_RDONLY)
                    if source != descriptor:
                        os.dup2(source, descriptor)
                        actual_close(source)
                    replacement.append(descriptor)
                    raise interruption
                return actual_close(descriptor)

            try:
                with mock.patch.object(
                    readiness_module.os,
                    "close",
                    side_effect=reuse_private_descriptor_then_interrupt,
                ):
                    readiness_module._abort_native_readiness_run(
                        None, roots, None, None
                    )
                self.assertEqual(len(replacement), 1)
                os.fstat(replacement[0])
                with self.assertRaises(OSError):
                    os.fstat(temp_fd)
                self.assertEqual(roots.private_fd, -1)
                self.assertEqual(roots.temp_fd, -1)
            finally:
                for descriptor in replacement + [private_fd, temp_fd]:
                    try:
                        actual_close(descriptor)
                    except OSError:
                        pass

    def test_terminal_publish_interrupt_restores_fallback_despite_close_error(
        self,
    ) -> None:
        blocked = {
            "cleanup_state": "removed",
            "reason_code": "evidence_retention_failed",
            "schema_version": 1,
            "status": "blocked",
        }
        success = {
            "cleanup_state": "removed",
            "reason_code": "native_primitive_observations_recorded",
            "schema_version": 1,
            "status": "native_primitive_observations_only",
        }
        with tempfile.TemporaryDirectory() as raw:
            private_root = Path(raw).resolve() / "private"
            private_root.mkdir(mode=0o700)
            root_fd = os.open(
                str(private_root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            root_info = os.fstat(root_fd)
            publisher = _EvidencePublisher(
                private_root,
                root_fd,
                (root_info.st_dev, root_info.st_ino),
            )
            os.close(root_fd)
            publisher.publish(blocked)
            interruption = KeyboardInterrupt("terminal verify interrupted")
            actual_verify = publisher._verify_descriptor
            actual_open = os.open
            actual_close = os.close
            published_descriptors = []

            def verify_then_interrupt(descriptor, expected):
                actual_verify(descriptor, expected)
                raise interruption

            def capture_open(path, *args, **kwargs):
                descriptor = actual_open(path, *args, **kwargs)
                if path == "readiness.json":
                    published_descriptors.append(descriptor)
                return descriptor

            def close_with_fault(descriptor):
                actual_close(descriptor)
                if (
                    published_descriptors
                    and descriptor == published_descriptors[0]
                ):
                    raise OSError(errno.EIO, "close fault")

            try:
                with mock.patch.object(
                    publisher,
                    "_verify_descriptor",
                    side_effect=verify_then_interrupt,
                ), mock.patch.object(
                    readiness_module.os,
                    "open",
                    side_effect=capture_open,
                ), mock.patch.object(
                    readiness_module.os,
                    "close",
                    side_effect=close_with_fault,
                ), self.assertRaises(KeyboardInterrupt) as raised:
                    publisher.publish(success, fallback=blocked)
                self.assertIs(raised.exception, interruption)
                self.assertEqual(
                    publisher.path.read_bytes(), canonical_bytes(blocked)
                )
                for artifact in publisher.run_directory.iterdir():
                    if artifact.is_file():
                        self.assertNotEqual(
                            artifact.read_bytes(), canonical_bytes(success)
                        )
            finally:
                publisher.close()

    def test_production_policy_literals_match_exact_contract(self) -> None:
        self.assertEqual(PERMISSION_PROFILE_NAME, "phase-b0-native-readonly")
        self.assertEqual(len(CONFIG_BYTES), 284)
        self.assertEqual(
            "sha256:" + hashlib.sha256(CONFIG_BYTES).hexdigest(),
            "sha256:74f108e8c6df73d7c045d5dd5453f795916a3b394b2c968dc7b63994fb41764a",
        )
        child_bytes = CHILD_SOURCE.encode("utf-8")
        self.assertEqual(len(child_bytes), 2489)
        self.assertEqual(
            "sha256:" + hashlib.sha256(child_bytes).hexdigest(),
            "sha256:aa1e8614fdd267c2cd0bbf237f1ed8f3a039e6a1f9de416cef5c52f3c520e6a9",
        )
        self.assertEqual(
            sha256_id(_PRODUCTION_POLICY.document),
            "sha256:af3a5979f7799bdf27f8ec100352a458b47e01464753d9f76e8f6826639e777c",
        )

    def test_policy_is_defensively_immutable_and_validates_literals(self) -> None:
        document = copy.deepcopy(_PRODUCTION_POLICY.document)
        policy = _NativeReadinessPolicy(document, CONFIG_BYTES, CHILD_SOURCE)
        document["limits"]["command_timeout_seconds"] = 999
        observed = policy.document
        observed["limits"]["command_timeout_seconds"] = 998
        self.assertEqual(
            policy.document["limits"]["command_timeout_seconds"], 10
        )
        with self.assertRaises(ValueError):
            _NativeReadinessPolicy(
                copy.deepcopy(_PRODUCTION_POLICY.document),
                CONFIG_BYTES + b"x",
                CHILD_SOURCE,
            )

    def test_result_accepts_only_exact_terminal_combinations(self) -> None:
        digest = "sha256:" + "a" * 64
        success = NativeCanaryReadinessResult(
            "native_primitive_observations_only",
            0,
            digest,
            digest,
            digest,
            digest,
            digest,
            digest,
            digest,
            "removed",
            "native_primitive_observations_recorded",
        )
        self.assertEqual(success.model_calls, 0)
        with self.assertRaises(ValueError):
            dataclasses.replace(success, model_calls=True)
        with self.assertRaises(ValueError):
            dataclasses.replace(success, reason_code="unknown")
        with self.assertRaises(ValueError):
            dataclasses.replace(success, complete_evidence_digest=None)
        with self.assertRaises(ValueError):
            dataclasses.replace(success, policy_digest=1)
        for model_calls in (-1, 1, False, True):
            with self.subTest(model_calls=model_calls), self.assertRaises(
                ValueError
            ):
                dataclasses.replace(success, model_calls=model_calls)
        for status in ("blocked", "unknown"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                dataclasses.replace(success, status=status)
        for cleanup in ("not_started", "cleanup_required", "unknown"):
            with self.subTest(cleanup=cleanup), self.assertRaises(ValueError):
                dataclasses.replace(success, cleanup_state=cleanup)

    def test_result_terminal_table_is_exhaustive(self) -> None:
        digest = "sha256:" + "b" * 64
        cases = (
            ("request_invalid", "blocked", "not_started", "1000000"),
            ("unsupported_platform", "blocked", "not_started", "1000000"),
            ("executable_identity_invalid", "blocked", "removed", "1000000"),
            ("cli_version_mismatch", "blocked", "removed", "1110000"),
            ("weak_control_invalid", "blocked", "removed", "1110000"),
            ("native_permission_unproven", "blocked", "removed", "1110000"),
            ("ledger_primitives_unproven", "blocked", "removed", "1111100"),
            ("evidence_retention_failed", "blocked", "removed", "1111110"),
            ("cleanup_required", "blocked", "cleanup_required", "1111111"),
        )
        for reason, status, cleanup, mask in cases:
            values = [digest if bit == "1" else None for bit in mask]
            with self.subTest(reason=reason):
                NativeCanaryReadinessResult(
                    status, 0, *values, cleanup, reason
                )
                with self.assertRaises(ValueError):
                    NativeCanaryReadinessResult(
                        status,
                        0,
                        *values,
                        "not_started" if cleanup == "cleanup_required" else "cleanup_required",
                        reason,
                    )
        for mask in ("1000000", "1110000", "1111100", "1111110", "1111111"):
            values = [digest if bit == "1" else None for bit in mask]
            NativeCanaryReadinessResult(
                "blocked",
                0,
                *values,
                "cleanup_required",
                "cleanup_required",
            )
        with self.assertRaises(ValueError):
            dataclasses.replace(
                NativeCanaryReadinessResult(
                    "blocked",
                    0,
                    digest,
                    digest,
                    digest,
                    None,
                    None,
                    None,
                    None,
                    "removed",
                    "weak_control_invalid",
                ),
                python_executable_identity_digest=None,
            )

    def test_every_unlisted_terminal_digest_mask_is_rejected(self) -> None:
        digest = "sha256:" + "c" * 64
        terminals = {
            "native_primitive_observations_recorded": (
                "native_primitive_observations_only",
                "removed",
                {"1111111"},
            ),
            "request_invalid": ("blocked", "not_started", {"1000000"}),
            "unsupported_platform": ("blocked", "not_started", {"1000000"}),
            "executable_identity_invalid": (
                "blocked",
                "removed",
                {"1000000"},
            ),
            "cli_version_mismatch": ("blocked", "removed", {"1110000"}),
            "weak_control_invalid": ("blocked", "removed", {"1110000"}),
            "native_permission_unproven": (
                "blocked",
                "removed",
                {"1110000"},
            ),
            "ledger_primitives_unproven": (
                "blocked",
                "removed",
                {"1111100"},
            ),
            "evidence_retention_failed": (
                "blocked",
                "removed",
                {"1111110"},
            ),
            "cleanup_required": (
                "blocked",
                "cleanup_required",
                {"1000000", "1110000", "1111100", "1111110", "1111111"},
            ),
        }
        for reason, (status, cleanup, allowed) in terminals.items():
            for raw_mask in range(128):
                mask = format(raw_mask, "07b")
                if mask in allowed:
                    continue
                values = [digest if bit == "1" else None for bit in mask]
                with self.subTest(reason=reason, mask=mask):
                    with self.assertRaises(ValueError):
                        NativeCanaryReadinessResult(
                            status, 0, *values, cleanup, reason
                        )

    def test_listener_failure_is_distinct_from_no_connection(self) -> None:
        class FailingSocket:
            def accept(self):
                raise OSError(errno.EIO, "fault")

            def close(self):
                pass

        listener = _LoopbackListener()
        listener._socket.close()
        listener._socket = FailingSocket()
        try:
            self.assertEqual(listener.observation(b"x"), "failure")
        finally:
            listener.close()

    def test_invalid_request_returns_before_runner(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            temp_parent = base / "temp"
            private_root = base / "private"
            temp_parent.mkdir(mode=0o700)
            private_root.mkdir(mode=0o700)
            result = _run_with_policy(
                NativeCanaryReadinessRequest(
                    Path(os.path.realpath(sys.executable)),
                    Path(os.path.realpath(sys.executable)),
                    temp_parent / ".." / "temp",
                    private_root,
                ),
                _PRODUCTION_POLICY,
                "darwin",
                calls.append,
            )
        self.assertEqual(result.reason_code, "request_invalid")
        self.assertEqual(calls, [])

    def test_public_entrypoint_has_no_runner_injection_and_uses_bounded_runner(
        self,
    ) -> None:
        self.assertEqual(
            tuple(
                inspect.signature(
                    readiness_module.run_native_canary_readiness
                ).parameters
            ),
            ("request",),
        )
        sentinel = mock.sentinel.result
        request = mock.sentinel.request
        with mock.patch.object(
            readiness_module, "_run_with_policy", return_value=sentinel
        ) as run_with_policy:
            result = readiness_module.run_native_canary_readiness(request)
        self.assertIs(result, sentinel)
        run_with_policy.assert_called_once_with(
            request,
            _PRODUCTION_POLICY,
            readiness_module.platform.system().lower(),
            readiness_module._run_bounded_command,
        )

    def test_same_physical_request_roots_are_rejected(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            temp_parent = base / "temp"
            private_root = base / "private"
            temp_parent.mkdir(mode=0o700)
            private_root.mkdir(mode=0o700)
            request = NativeCanaryReadinessRequest(
                Path(os.path.realpath(sys.executable)),
                Path(os.path.realpath(sys.executable)),
                temp_parent,
                private_root,
            )
            with mock.patch.object(
                readiness_module,
                "_validate_root_fd",
                side_effect=((123, 456), (123, 456)),
            ):
                result = _run_with_policy(
                    request, _PRODUCTION_POLICY, "darwin", calls.append
                )
        self.assertEqual(result.reason_code, "request_invalid")
        self.assertEqual(calls, [])

    def test_unsupported_platform_returns_before_runner(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            os.chmod(base, 0o700)
            temp_parent = base / "temp"
            private_root = base / "private"
            temp_parent.mkdir(mode=0o700)
            private_root.mkdir(mode=0o700)
            result = _run_with_policy(
                NativeCanaryReadinessRequest(
                    Path(os.path.realpath(sys.executable)),
                    Path(os.path.realpath(sys.executable)),
                    temp_parent,
                    private_root,
                ),
                _PRODUCTION_POLICY,
                "linux",
                calls.append,
            )
        self.assertEqual(result.reason_code, "unsupported_platform")
        self.assertEqual(calls, [])

    def test_untrusted_request_shapes_do_not_invoke_runner(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            executable = Path(os.path.realpath(sys.executable))
            temp_parent = base / "temp"
            private_root = base / "private"
            temp_parent.mkdir(mode=0o700)
            private_root.mkdir(mode=0o700)
            request = NativeCanaryReadinessRequest(
                executable, executable, temp_parent, private_root
            )
            occupied = temp_parent / "occupied"
            occupied.write_bytes(b"x")
            result = _run_with_policy(
                request, _PRODUCTION_POLICY, "darwin", calls.append
            )
            self.assertEqual(result.reason_code, "request_invalid")
            occupied.unlink()
            os.chmod(str(private_root), 0o755)
            result = _run_with_policy(
                request, _PRODUCTION_POLICY, "darwin", calls.append
            )
            self.assertEqual(result.reason_code, "request_invalid")
            os.chmod(str(private_root), 0o700)
            result = _run_with_policy(
                NativeCanaryReadinessRequest(
                    executable, executable, temp_parent, temp_parent
                ),
                _PRODUCTION_POLICY,
                "darwin",
                calls.append,
            )
            self.assertEqual(result.reason_code, "request_invalid")
            alias = base / "temp-alias"
            alias.symlink_to(temp_parent, target_is_directory=True)
            result = _run_with_policy(
                NativeCanaryReadinessRequest(
                    executable, executable, alias, private_root
                ),
                _PRODUCTION_POLICY,
                "darwin",
                calls.append,
            )
            self.assertEqual(result.reason_code, "request_invalid")
        self.assertEqual(calls, [])

    def test_bounded_runner_executes_with_exact_environment(self) -> None:
        command = BoundedCommand(
            (sys.executable, "-I", "-S", "-B", "-c", "import os;print(os.environ['ONLY'])"),
            Path.cwd(),
            {"ONLY": "present"},
            2,
            100,
            100,
        )
        result = _run_bounded_command(command)
        self.assertEqual(
            result,
            BoundedCommandResult(0, b"present\n", b"", False, False),
        )

    def test_bounded_runner_caps_output(self) -> None:
        result = _run_bounded_command(
            BoundedCommand(
                (sys.executable, "-I", "-S", "-B", "-c", "print('x' * 10000)"),
                Path.cwd(),
                {},
                2,
                64,
                64,
            )
        )
        self.assertTrue(result.output_overflow)
        self.assertLessEqual(len(result.stdout), 64)

    def test_bounded_runner_enforces_deadline(self) -> None:
        result = _run_bounded_command(
            BoundedCommand(
                (sys.executable, "-I", "-S", "-B", "-c", "import time;time.sleep(5)"),
                Path.cwd(),
                {},
                1,
                64,
                64,
            )
        )
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)

    def test_bounded_runner_interrupt_after_spawn_reaps_and_closes_resources(
        self,
    ) -> None:
        selector = readiness_module.selectors.DefaultSelector()
        actual_popen = subprocess.Popen
        spawned = []

        def capture_popen(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            spawned.append(process)
            return process

        command = BoundedCommand(
            (
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                "import time;time.sleep(10)",
            ),
            Path.cwd(),
            {},
            2,
            64,
            64,
        )
        interruption = KeyboardInterrupt("bounded runner interrupted")
        try:
            with mock.patch.object(
                readiness_module.subprocess,
                "Popen",
                side_effect=capture_popen,
            ), mock.patch.object(
                readiness_module.selectors,
                "DefaultSelector",
                return_value=selector,
            ), mock.patch.object(
                selector,
                "select",
                side_effect=interruption,
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    _run_bounded_command(command)
            self.assertIs(raised.exception, interruption)
            self.assertEqual(len(spawned), 1)
            process = spawned[0]
            self.assertIsNotNone(process.poll())
            with self.assertRaises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            self.assertIsNone(selector.get_map())
        finally:
            if spawned and spawned[0].poll() is None:
                try:
                    os.killpg(spawned[0].pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                spawned[0].wait()
            selector.close()

    def test_bounded_runner_preserves_selector_close_base_exception(
        self,
    ) -> None:
        before = len(os.listdir("/dev/fd"))
        selector = readiness_module.selectors.DefaultSelector()
        actual_selector_close = selector.close
        actual_popen = subprocess.Popen
        spawned = []
        interruption = KeyboardInterrupt("selector close interrupted")

        def capture_popen(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            spawned.append(process)
            return process

        def close_then_interrupt():
            actual_selector_close()
            raise interruption

        command = BoundedCommand(
            (
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                "print('done')",
            ),
            Path.cwd(),
            {},
            2,
            64,
            64,
        )
        with mock.patch.object(
            readiness_module.subprocess,
            "Popen",
            side_effect=capture_popen,
        ), mock.patch.object(
            readiness_module.selectors,
            "DefaultSelector",
            return_value=selector,
        ), mock.patch.object(
            selector,
            "close",
            side_effect=close_then_interrupt,
        ), self.assertRaises(KeyboardInterrupt) as raised:
            _run_bounded_command(command)
        self.assertIs(raised.exception, interruption)
        self.assertEqual(len(spawned), 1)
        self.assertTrue(spawned[0].stdout.closed)
        self.assertTrue(spawned[0].stderr.closed)
        self.assertIsNone(selector.get_map())
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_bounded_runner_kills_pipe_holding_descendant_after_leader_exit(self) -> None:
        source = (
            "import signal,subprocess,sys;"
            "child=subprocess.Popen([sys.executable,'-I','-S','-B','-c',"
            "'import time;time.sleep(10)'],"
            "preexec_fn=lambda:signal.signal(signal.SIGTERM,signal.SIG_IGN));"
            "print(child.pid,flush=True)"
        )
        started = time.monotonic()
        descendant_pid = None
        try:
            result = _run_bounded_command(
                BoundedCommand(
                    (sys.executable, "-I", "-S", "-B", "-c", source),
                    Path.cwd(),
                    {},
                    1,
                    64,
                    64,
                )
            )
            elapsed = time.monotonic() - started
            self.assertTrue(result.timed_out)
            self.assertLess(elapsed, 2.0)
            descendant_pid = int(result.stdout)
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    os.kill(descendant_pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    self.fail("same-group descendant survived timeout cleanup")
                time.sleep(0.01)
        finally:
            if descendant_pid is not None:
                try:
                    os.kill(descendant_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_bounded_runner_fails_closed_when_leader_exits_with_live_group(
        self,
    ) -> None:
        source = (
            "import subprocess,sys;"
            "child=subprocess.Popen([sys.executable,'-I','-S','-B','-c',"
            "'import time;time.sleep(10)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            "print(child.pid,flush=True)"
        )
        descendant_pid = None
        try:
            result = _run_bounded_command(
                BoundedCommand(
                    (sys.executable, "-I", "-S", "-B", "-c", source),
                    Path.cwd(),
                    {},
                    2,
                    64,
                    64,
                )
            )
            descendant_pid = int(result.stdout)
            self.assertTrue(result.timed_out)
            self.assertIsNone(result.returncode)
            self.assertFalse(result.output_overflow)
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    os.kill(descendant_pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    self.fail("same-group descendant survived leader cleanup")
                time.sleep(0.01)
        finally:
            if descendant_pid is not None:
                try:
                    os.kill(descendant_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_terminate_group_preserves_eperm_while_attempting_all_steps(
        self,
    ) -> None:
        process = mock.Mock(pid=12345, returncode=0)
        permission_error = PermissionError(errno.EPERM, "denied")
        with mock.patch.object(
            readiness_module.os,
            "killpg",
            side_effect=permission_error,
        ) as killpg, self.assertRaises(PermissionError) as raised:
            readiness_module._terminate_group(process)
        self.assertIs(raised.exception, permission_error)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGKILL),
            ],
        )

    def test_sealed_executable_rejections_do_not_leak_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw).resolve() / "tool"
            executable.write_bytes(b"tool")
            executable.chmod(0o755)
            specification = {
                "expected_mode": "0755",
                "expected_owner": "operator_uid",
                "sha256": "sha256:" + "0" * 64,
            }
            limits = {
                "executable_hash_chunk_bytes": 1024,
                "max_executable_bytes": 1024,
            }
            before = len(os.listdir("/dev/fd"))
            for _ in range(64):
                with self.assertRaises(readiness_module._IdentityError):
                    _SealedExecutable(executable, specification, limits)
            self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_sealed_executable_closes_descriptor_on_hash_base_exception(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw).resolve() / "tool"
            executable.write_bytes(b"tool")
            executable.chmod(0o755)
            specification = {
                "expected_mode": "0755",
                "expected_owner": "operator_uid",
                "sha256": "sha256:" + hashlib.sha256(b"tool").hexdigest(),
            }
            limits = {
                "executable_hash_chunk_bytes": 1024,
                "max_executable_bytes": 1024,
            }
            actual_open = os.open
            opened = []
            interruption = KeyboardInterrupt("hash interrupted")

            def capture_open(*args, **kwargs):
                descriptor = actual_open(*args, **kwargs)
                opened.append(descriptor)
                return descriptor

            with mock.patch.object(
                readiness_module.os, "open", side_effect=capture_open
            ), mock.patch.object(
                _SealedExecutable,
                "_hash_descriptor",
                side_effect=interruption,
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    _SealedExecutable(executable, specification, limits)
            self.assertIs(raised.exception, interruption)
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):
                os.fstat(opened[0])

    def test_lock_probe_parent_bounds_silent_child(self) -> None:
        read_fd, write_fd = os.pipe()
        process_id = os.fork()
        if process_id == 0:
            os.close(read_fd)
            try:
                time.sleep(10)
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        started = time.monotonic()
        with self.assertRaises(PrivateJSONLLedgerError):
            _wait_for_lock_probe(process_id, read_fd, 0.25)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_lock_probe_reaps_prompt_child_once_while_descendant_holds_pipe(self) -> None:
        read_fd, write_fd = os.pipe()
        process_id = os.fork()
        if process_id == 0:
            os.close(read_fd)
            descendant = os.fork()
            if descendant == 0:
                try:
                    time.sleep(0.25)
                finally:
                    os.close(write_fd)
                    os._exit(0)
            os.write(write_fd, b"1")
            os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        started = time.monotonic()
        observed, status_value = _wait_for_lock_probe(
            process_id, read_fd, 1.0
        )
        self.assertEqual(observed, b"1")
        self.assertEqual(status_value, 0)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_linux_private_evaluator_records_real_observations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            temp_parent = base / "temp"
            private_root = base / "private"
            binaries = base / "bin"
            for directory in (temp_parent, private_root, binaries):
                directory.mkdir(mode=0o700)
            codex = binaries / "codex"
            python = binaries / "python"
            codex.write_bytes(b"#!/bin/sh\nexit 0\n")
            python.write_bytes(b"#!/bin/sh\nexit 0\n")
            codex.chmod(0o755)
            python.chmod(0o755)
            document = copy.deepcopy(_PRODUCTION_POLICY.document)
            document["platform_family"] = "linux"
            document["executables"]["codex"].update(
                sha256="sha256:" + hashlib.sha256(codex.read_bytes()).hexdigest(),
                version_line="codex-cli test",
            )
            document["executables"]["python"].update(
                expected_owner="operator_uid",
                sha256="sha256:" + hashlib.sha256(python.read_bytes()).hexdigest(),
                version_line="Python test",
            )
            policy = _NativeReadinessPolicy(document, CONFIG_BYTES, CHILD_SOURCE)
            seen = []
            scenario = {"mode": "success"}

            def runner(command):
                seen.append(command)
                if command.argv[-1] == "--version":
                    if (
                        scenario["mode"] == "cli_version_mismatch"
                        and command.argv[0] == str(codex)
                    ):
                        return BoundedCommandResult(0, b"wrong\n", b"", False, False)
                    line = b"codex-cli test\n" if command.argv[0] == str(codex) else b"Python test\n"
                    if (
                        scenario["mode"] == "executable_replaced"
                        and command.argv[0] == str(codex)
                    ):
                        original = codex.with_name("codex-original")
                        codex.rename(original)
                        codex.write_bytes(original.read_bytes())
                        codex.chmod(0o755)
                    return BoundedCommandResult(0, line, b"", False, False)
                token = bytes.fromhex(command.argv[-1])
                weak = command.argv[0] == str(python)
                if weak and scenario["mode"] == "weak_control_invalid":
                    return BoundedCommandResult(0, b"{}\n", b"", False, False)
                if weak and scenario["mode"] == "malformed_runner":
                    return object()
                if weak and scenario["mode"] == "timeout":
                    return BoundedCommandResult(None, b"", b"", True, False)
                if weak and scenario["mode"] == "output_overflow":
                    return BoundedCommandResult(-15, b"x", b"", False, True)
                if weak and scenario["mode"] == "signal":
                    return BoundedCommandResult(-9, b"", b"", False, False)
                if weak and scenario["mode"] == "internal_exception":
                    raise RuntimeError("credential-like-output")
                if weak:
                    with socket.create_connection(("127.0.0.1", int(command.argv[-2]))) as client:
                        client.sendall(token)
                    Path(command.argv[-4]).write_bytes(token)
                    Path(command.argv[-3]).write_bytes(token)
                    Path(command.argv[-4]).chmod(0o600)
                    Path(command.argv[-3]).chmod(0o600)
                    if scenario["mode"] == "read_sentinel_replaced":
                        allowed_read = Path(command.argv[-6])
                        moved_read = allowed_read.with_name("allowed-read-original")
                        allowed_read.rename(moved_read)
                        allowed_read.write_bytes(moved_read.read_bytes())
                        allowed_read.chmod(0o600)
                allowed_digest = hashlib.sha256(Path(command.argv[-6]).read_bytes()).hexdigest()
                forbidden_digest = hashlib.sha256(Path(command.argv[-5]).read_bytes()).hexdigest()
                payload = {
                    "allowed_read": {"digest": allowed_digest, "errno": None, "ok": True, "overflow": False},
                    "allowed_write": {"errno": None if weak else errno.EPERM, "ok": weak},
                    "environment": {
                        "digest": hashlib.sha256(command.environment["PHASE_B0_SYNTHETIC_SECRET"].encode()).hexdigest() if weak else None,
                        "present": weak,
                    },
                    "forbidden_read": {"digest": forbidden_digest if weak else None, "errno": None if weak else errno.EACCES, "ok": weak, "overflow": False},
                    "forbidden_write": {"errno": None if weak else errno.EPERM, "ok": weak},
                    "network": {"errno": None if weak else errno.EPERM, "ok": weak},
                    "schema_version": 1,
                }
                false_network_causes = {
                    "native_permission_unproven": errno.ECONNREFUSED,
                    "network_refused": errno.ECONNREFUSED,
                    "network_reset": errno.ECONNRESET,
                    "network_timeout": errno.ETIMEDOUT,
                }
                if not weak and scenario["mode"] in false_network_causes:
                    payload["network"] = {
                        "errno": false_network_causes[scenario["mode"]],
                        "ok": False,
                    }
                if not weak and scenario["mode"] == "listener_mismatch":
                    with socket.create_connection(
                        ("127.0.0.1", int(command.argv[-2]))
                    ) as client:
                        client.sendall(bytes([token[0] ^ 1]) + token[1:])
                if not weak and scenario["mode"] == "evidence_retention_failed":
                    os.chmod(str(private_root), 0o500)
                if not weak and scenario["mode"] == "private_root_replaced":
                    moved_root = private_root.with_name("private-original")
                    private_root.rename(moved_root)
                    private_root.mkdir(mode=0o700)
                if not weak and scenario["mode"] == "cleanup_required":
                    temporary = Path(command.environment["TMPDIR"])
                    moved = temporary.with_name(temporary.name + "-moved")
                    temporary.rename(moved)
                    temporary.mkdir(mode=0o700)
                if not weak and scenario["mode"] == "denied_output_symlink":
                    Path(command.argv[-4]).symlink_to("missing-output")
                if not weak and scenario["mode"] == "config_replaced":
                    config = Path(command.environment["CODEX_HOME"]) / "config.toml"
                    original_config = config.with_name("config-original.toml")
                    config.rename(original_config)
                    config.write_bytes(original_config.read_bytes())
                    config.chmod(0o600)
                if not weak and scenario["mode"] == "codex_home_replaced":
                    codex_home = Path(command.environment["CODEX_HOME"])
                    original_home = codex_home.with_name("codex-home-original")
                    codex_home.rename(original_home)
                    codex_home.mkdir(mode=0o700)
                    replacement_config = codex_home / "config.toml"
                    replacement_config.write_bytes(CONFIG_BYTES)
                    replacement_config.chmod(0o600)
                if scenario["mode"] == "extra_child_field":
                    payload["extra"] = "rejected"
                return BoundedCommandResult(
                    0,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n",
                    b"",
                    False,
                    False,
                )

            result = _run_with_policy(
                NativeCanaryReadinessRequest(codex, python, temp_parent, private_root),
                policy,
                "linux",
                runner,
            )
            self.assertEqual(result.reason_code, "native_primitive_observations_recorded")
            self.assertEqual(len(seen), 4)
            retained = list(private_root.glob("*/readiness.json"))
            self.assertEqual(len(retained), 1)
            retained_bytes = retained[0].read_bytes()
            self.assertEqual(
                result.complete_evidence_digest,
                "sha256:" + hashlib.sha256(retained_bytes).hexdigest(),
            )
            synthetic_value = next(
                command.environment["PHASE_B0_SYNTHETIC_SECRET"].encode()
                for command in seen
                if "PHASE_B0_SYNTHETIC_SECRET" in command.environment
            )
            for secret in (
                str(base).encode(),
                b"PHASE_B0_SYNTHETIC_SECRET",
                synthetic_value,
                b"codex-cli test",
            ):
                self.assertNotIn(secret, retained_bytes)
            self.assertEqual(retained[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(retained[0].parent.stat().st_mode & 0o777, 0o700)
            poison = (
                "codex exec",
                "preflight_auth",
                "RuntimeContainmentReceipt",
                "codex features list",
                "model_client",
                "provider_client",
                "http://",
                "https://",
                "external_destination",
                "runtime_receipt",
                "runtime_terminal",
                "pilot_marker",
                "marker_path",
                "reservation.json",
                "create_reservation",
                "lima ",
                "multipass",
                " vm ",
            )
            source = Path(__file__).parents[1] / "scripts/live_eval/native_canary_readiness.py"
            source_text = source.read_text()
            for phrase in poison:
                self.assertNotIn(phrase, source_text)
                self.assertFalse(any(phrase in " ".join(command.argv) for command in seen))

            expected_modes = (
                ("cli_version_mismatch", policy),
                ("weak_control_invalid", policy),
                ("native_permission_unproven", policy),
                ("network_refused", policy),
                ("network_reset", policy),
                ("network_timeout", policy),
                ("listener_mismatch", policy),
                ("malformed_runner", policy),
                ("timeout", policy),
                ("output_overflow", policy),
                ("signal", policy),
                ("internal_exception", policy),
                ("extra_child_field", policy),
                ("read_sentinel_replaced", policy),
                ("denied_output_symlink", policy),
                ("config_replaced", policy),
                ("codex_home_replaced", policy),
            )
            for mode, scenario_policy in expected_modes:
                scenario["mode"] = mode
                with self.subTest(mode=mode):
                    blocked = _run_with_policy(
                        NativeCanaryReadinessRequest(
                            codex, python, temp_parent, private_root
                        ),
                        scenario_policy,
                        "linux",
                        runner,
                    )
                    expected_reason = (
                        "native_permission_unproven"
                        if mode
                        in {
                            "listener_mismatch",
                            "network_refused",
                            "network_reset",
                            "network_timeout",
                        }
                        else (
                            "weak_control_invalid"
                            if mode
                            in {
                                "malformed_runner",
                                "timeout",
                                "output_overflow",
                                "signal",
                                "internal_exception",
                                "extra_child_field",
                                "read_sentinel_replaced",
                            }
                            else mode
                        )
                    )
                    if mode == "denied_output_symlink":
                        expected_reason = "native_permission_unproven"
                    if mode in {"config_replaced", "codex_home_replaced"}:
                        expected_reason = "native_permission_unproven"
                    self.assertEqual(blocked.reason_code, expected_reason)
                    self.assertEqual(blocked.model_calls, 0)
                    public_bytes = canonical_bytes(dataclasses.asdict(blocked))
                    self.assertNotIn(b"credential-like-output", public_bytes)

            ledger_document = copy.deepcopy(document)
            ledger_document["ledger"]["max_records"] = 1
            scenario["mode"] = "success"
            blocked = _run_with_policy(
                NativeCanaryReadinessRequest(codex, python, temp_parent, private_root),
                _NativeReadinessPolicy(ledger_document, CONFIG_BYTES, CHILD_SOURCE),
                "linux",
                runner,
            )
            self.assertEqual(blocked.reason_code, "ledger_primitives_unproven")

            scenario["mode"] = "evidence_retention_failed"
            blocked = _run_with_policy(
                NativeCanaryReadinessRequest(codex, python, temp_parent, private_root),
                policy,
                "linux",
                runner,
            )
            self.assertEqual(blocked.reason_code, "evidence_retention_failed")
            os.chmod(str(private_root), 0o700)

            scenario["mode"] = "private_root_replaced"
            blocked = _run_with_policy(
                NativeCanaryReadinessRequest(codex, python, temp_parent, private_root),
                policy,
                "linux",
                runner,
            )
            self.assertEqual(blocked.reason_code, "evidence_retention_failed")
            self.assertEqual(list(private_root.iterdir()), [])
            private_root.rmdir()
            private_root.with_name("private-original").rename(private_root)

            scenario["mode"] = "cleanup_required"
            blocked = _run_with_policy(
                NativeCanaryReadinessRequest(codex, python, temp_parent, private_root),
                policy,
                "linux",
                runner,
            )
            self.assertEqual(blocked.reason_code, "cleanup_required")
            for leftover in tuple(temp_parent.iterdir()):
                shutil.rmtree(str(leftover))

            scenario["mode"] = "executable_replaced"
            blocked = _run_with_policy(
                NativeCanaryReadinessRequest(codex, python, temp_parent, private_root),
                policy,
                "linux",
                runner,
            )
            self.assertEqual(blocked.reason_code, "executable_identity_invalid")
            codex.unlink()
            codex.with_name("codex-original").rename(codex)

            invalid_document = copy.deepcopy(document)
            invalid_document["executables"]["codex"]["sha256"] = "sha256:" + "0" * 64
            scenario["mode"] = "success"
            calls_before = len(seen)
            blocked = _run_with_policy(
                NativeCanaryReadinessRequest(codex, python, temp_parent, private_root),
                _NativeReadinessPolicy(invalid_document, CONFIG_BYTES, CHILD_SOURCE),
                "linux",
                runner,
            )
            self.assertEqual(blocked.reason_code, "executable_identity_invalid")
            self.assertEqual(len(seen), calls_before)

            request = NativeCanaryReadinessRequest(
                codex, python, temp_parent, private_root
            )
            private_before = {path.name for path in private_root.iterdir()}
            interruption = KeyboardInterrupt("publisher stage interrupted")
            created_publishers = []
            actual_publisher_class = _EvidencePublisher

            def capture_publisher(*args, **kwargs):
                created = actual_publisher_class(*args, **kwargs)
                created_publishers.append(created)
                return created

            with self.subTest(fault="publisher_stage_interrupt"):
                try:
                    with mock.patch.object(
                        readiness_module,
                        "_EvidencePublisher",
                        side_effect=capture_publisher,
                    ), mock.patch.object(
                        actual_publisher_class,
                        "_stage",
                        side_effect=interruption,
                    ), self.assertRaises(KeyboardInterrupt) as raised:
                        _run_with_policy(
                            request, policy, "linux", runner
                        )
                    self.assertIs(raised.exception, interruption)
                    self.assertEqual(list(temp_parent.iterdir()), [])
                    self.assertEqual(
                        {path.name for path in private_root.iterdir()},
                        private_before,
                    )
                finally:
                    for created in created_publishers:
                        created.close()
                    for leftover in tuple(temp_parent.iterdir()):
                        shutil.rmtree(str(leftover))
                    for leftover in tuple(private_root.iterdir()):
                        if leftover.name not in private_before:
                            shutil.rmtree(str(leftover))

            interruption = KeyboardInterrupt("tree cleanup interrupted")
            original_remove_tree = readiness_module._remove_tree
            remove_calls = {"count": 0}
            created_publishers = []

            def interrupt_remove_once(*args, **kwargs):
                remove_calls["count"] += 1
                if remove_calls["count"] == 1:
                    raise interruption
                return original_remove_tree(*args, **kwargs)

            with self.subTest(fault="remove_tree_interrupt"):
                try:
                    with mock.patch.object(
                        readiness_module,
                        "_EvidencePublisher",
                        side_effect=capture_publisher,
                    ), mock.patch.object(
                        readiness_module,
                        "_remove_tree",
                        side_effect=interrupt_remove_once,
                    ), self.assertRaises(KeyboardInterrupt) as raised:
                        _run_with_policy(
                            request, policy, "linux", runner
                        )
                    self.assertIs(raised.exception, interruption)
                    self.assertGreaterEqual(remove_calls["count"], 2)
                    self.assertEqual(list(temp_parent.iterdir()), [])
                    self.assertEqual(
                        {path.name for path in private_root.iterdir()},
                        private_before,
                    )
                finally:
                    for created in created_publishers:
                        created.close()
                    for leftover in tuple(temp_parent.iterdir()):
                        shutil.rmtree(str(leftover))
                    for leftover in tuple(private_root.iterdir()):
                        if leftover.name not in private_before:
                            shutil.rmtree(str(leftover))

            interruption = KeyboardInterrupt("resource close interrupted")
            actual_registry_close = _ResourceRegistry.close
            actual_validate_request = readiness_module._validate_request
            created_publishers = []
            created_roots = []
            before_fds = len(os.listdir("/dev/fd"))

            def close_resources_then_interrupt(registry):
                actual_registry_close(registry)
                raise interruption

            def capture_roots(value):
                roots = actual_validate_request(value)
                if roots is not None:
                    created_roots.append(roots)
                return roots

            with self.subTest(fault="resource_close_interrupt"):
                try:
                    with mock.patch.object(
                        readiness_module,
                        "_EvidencePublisher",
                        side_effect=capture_publisher,
                    ), mock.patch.object(
                        readiness_module,
                        "_validate_request",
                        side_effect=capture_roots,
                    ), mock.patch.object(
                        _ResourceRegistry,
                        "close",
                        autospec=True,
                        side_effect=close_resources_then_interrupt,
                    ), self.assertRaises(KeyboardInterrupt) as raised:
                        _run_with_policy(
                            request, policy, "linux", runner
                        )
                    self.assertIs(raised.exception, interruption)
                    self.assertEqual(list(temp_parent.iterdir()), [])
                    self.assertEqual(
                        {path.name for path in private_root.iterdir()},
                        private_before,
                    )
                    self.assertEqual(len(os.listdir("/dev/fd")), before_fds)
                finally:
                    for created in created_publishers:
                        created.close()
                    for roots in created_roots:
                        roots.close()
                    for leftover in tuple(temp_parent.iterdir()):
                        shutil.rmtree(str(leftover))
                    for leftover in tuple(private_root.iterdir()):
                        if leftover.name not in private_before:
                            shutil.rmtree(str(leftover))

            interruption = KeyboardInterrupt("executable close interrupted")
            created_publishers = []
            created_roots = []
            replacement = []
            actual_close = os.close
            actual_open = os.open
            codex_info = os.stat(str(codex), follow_symlinks=False)
            codex_identity = (codex_info.st_dev, codex_info.st_ino)
            before_fds = len(os.listdir("/dev/fd"))

            def reuse_codex_descriptor_then_interrupt(descriptor):
                info = os.fstat(descriptor)
                if (
                    (info.st_dev, info.st_ino) == codex_identity
                    and not replacement
                ):
                    actual_close(descriptor)
                    source = actual_open("/dev/null", os.O_RDONLY)
                    if source != descriptor:
                        os.dup2(source, descriptor)
                        actual_close(source)
                    replacement.append(descriptor)
                    raise interruption
                return actual_close(descriptor)

            with self.subTest(fault="executable_close_after_success"):
                try:
                    with mock.patch.object(
                        readiness_module,
                        "_EvidencePublisher",
                        side_effect=capture_publisher,
                    ), mock.patch.object(
                        readiness_module,
                        "_validate_request",
                        side_effect=capture_roots,
                    ), mock.patch.object(
                        readiness_module.os,
                        "close",
                        side_effect=reuse_codex_descriptor_then_interrupt,
                    ), self.assertRaises(KeyboardInterrupt) as raised:
                        _run_with_policy(
                            request, policy, "linux", runner
                        )
                    self.assertIs(raised.exception, interruption)
                    self.assertEqual(len(replacement), 1)
                    os.fstat(replacement[0])
                    self.assertEqual(list(temp_parent.iterdir()), [])
                    self.assertEqual(
                        {path.name for path in private_root.iterdir()},
                        private_before,
                    )
                    actual_close(replacement.pop())
                    self.assertEqual(len(os.listdir("/dev/fd")), before_fds)
                finally:
                    for descriptor in replacement:
                        try:
                            actual_close(descriptor)
                        except OSError:
                            pass
                    for created in created_publishers:
                        created.close()
                    for roots in created_roots:
                        roots.close()
                    for leftover in tuple(temp_parent.iterdir()):
                        shutil.rmtree(str(leftover))
                    for leftover in tuple(private_root.iterdir()):
                        if leftover.name not in private_before:
                            shutil.rmtree(str(leftover))

    def test_private_runner_interrupt_cleans_temporary_and_private_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            temp_parent = base / "temp"
            private_root = base / "private"
            binaries = base / "bin"
            for directory in (temp_parent, private_root, binaries):
                directory.mkdir(mode=0o700)
            codex = binaries / "codex"
            python = binaries / "python"
            codex.write_bytes(b"#!/bin/sh\nexit 0\n")
            python.write_bytes(b"#!/bin/sh\nexit 0\n")
            codex.chmod(0o755)
            python.chmod(0o755)
            document = copy.deepcopy(_PRODUCTION_POLICY.document)
            document["platform_family"] = "linux"
            document["executables"]["codex"].update(
                sha256=(
                    "sha256:" + hashlib.sha256(codex.read_bytes()).hexdigest()
                ),
                version_line="codex-cli test",
            )
            document["executables"]["python"].update(
                expected_owner="operator_uid",
                sha256=(
                    "sha256:" + hashlib.sha256(python.read_bytes()).hexdigest()
                ),
                version_line="Python test",
            )
            policy = _NativeReadinessPolicy(
                document, CONFIG_BYTES, CHILD_SOURCE
            )

            interruption = KeyboardInterrupt("runner interrupted")

            def interrupt_runner(command):
                raise interruption

            with self.assertRaises(KeyboardInterrupt) as raised:
                _run_with_policy(
                    NativeCanaryReadinessRequest(
                        codex, python, temp_parent, private_root
                    ),
                    policy,
                    "linux",
                    interrupt_runner,
                )
            self.assertIs(raised.exception, interruption)
            self.assertEqual(list(temp_parent.iterdir()), [])
            self.assertEqual(list(private_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

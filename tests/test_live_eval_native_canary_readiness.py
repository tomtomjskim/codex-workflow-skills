"""Tests for native canary readiness observations."""

import dataclasses
import copy
import errno
import hashlib
import json
import os
import shutil
import socket
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

    def test_bounded_runner_kills_pipe_holding_descendant_after_leader_exit(self) -> None:
        source = (
            "import subprocess,sys;"
            "subprocess.Popen([sys.executable,'-I','-S','-B','-c',"
            "'import time;time.sleep(10)'])"
        )
        started = time.monotonic()
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


if __name__ == "__main__":
    unittest.main()

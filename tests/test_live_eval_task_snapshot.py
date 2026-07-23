import os
from pathlib import Path
import subprocess
import tempfile
import errno
import io
import socket
import unittest
from dataclasses import replace
from unittest.mock import patch

import scripts.live_eval.task_snapshot as task_snapshot_module
from scripts.live_eval.task_snapshot import (
    ObjectTopologySeal,
    PreparedTaskSource,
    TaskSnapshotError,
    TaskSnapshotPolicy,
    TaskSourceSpec,
    _capture_filesystem,
    _capture_object_topology,
    _cleanup_process,
    _open_root_descriptor,
    _parse_config_output,
    _parse_packed_refs,
    _process_policy_digest,
    _require_supported_platform,
    _run_git,
    _source_identity_digest,
    _open_child_directory,
    _validate_physical_root,
    prepare_task_source,
)


class RepositoryFixture:
    def make_repository(self, object_format="sha1"):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name).resolve()
        repo = root / "repo"
        repo.mkdir(mode=0o700)
        subprocess.run(
            ("git", "init", "-q", "--object-format=" + object_format),
            cwd=str(repo),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        (repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(
            (
                "git",
                "-c",
                "user.name=Task Snapshot",
                "-c",
                "user.email=snapshot@example.invalid",
                "add",
                "tracked.txt",
            ),
            cwd=str(repo),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            (
                "git",
                "-c",
                "user.name=Task Snapshot",
                "-c",
                "user.email=snapshot@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ),
            cwd=str(repo),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        oid = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=str(repo),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
        return repo, oid

    def source_for(self, repo, oid):
        return TaskSourceSpec(
            input_digest="sha256:" + "1" * 64,
            task_id="task-1",
            repository_root=repo,
            commit_oid=oid,
            provisioning_class="operator_owned_trusted_git_local_clone",
            operator_attested=True,
            local_clone_policy="remote_or_no_local_or_no_hardlinks",
        )


class TaskSnapshotSurfaceTests(unittest.TestCase):
    def test_exact_source_and_policy_defaults(self):
        source = TaskSourceSpec(
            input_digest="sha256:" + "1" * 64,
            task_id="task-1",
            repository_root=Path("/tmp/repo"),
            commit_oid="a" * 40,
            provisioning_class="operator_owned_trusted_git_local_clone",
            operator_attested=True,
            local_clone_policy="remote_or_no_local_or_no_hardlinks",
        )

        self.assertEqual(source.repository_root, Path("/tmp/repo"))
        self.assertEqual(
            TaskSnapshotPolicy().policy_version, "task-object-materializer-v1"
        )

    def test_source_scalars_and_policy_are_exact_and_bounded(self):
        base = {
            "input_digest": "sha256:" + "1" * 64,
            "task_id": "task-1",
            "repository_root": Path("/tmp/repo"),
            "commit_oid": "a" * 40,
            "provisioning_class": "operator_owned_trusted_git_local_clone",
            "operator_attested": True,
            "local_clone_policy": "remote_or_no_local_or_no_hardlinks",
        }
        attacks = (
            ("input_digest", "sha256:" + "A" * 64),
            ("task_id", "Task 1"),
            ("repository_root", b"/tmp/repo"),
            ("commit_oid", "a" * 39),
            ("provisioning_class", "local"),
            ("operator_attested", 1),
            ("local_clone_policy", "hardlinks"),
        )
        for field_name, value in attacks:
            with self.subTest(field_name=field_name), self.assertRaisesRegex(
                TaskSnapshotError, "^task_source_spec_invalid$"
            ):
                TaskSourceSpec(**dict(base, **{field_name: value}))

        policy_attacks = (
            {"git_timeout_seconds": True},
            {"git_timeout_seconds": 0},
            {"git_timeout_seconds": 16},
            {"max_git_stderr_bytes": 70000, "max_git_stdout_bytes": 65536},
            {"max_file_bytes": 1024, "max_total_bytes": 512},
        )
        for values in policy_attacks:
            with self.subTest(values=values), self.assertRaisesRegex(
                TaskSnapshotError, "^task_snapshot_policy_invalid$"
            ):
                TaskSnapshotPolicy(**values)

    def test_platform_and_physical_root_fail_closed(self):
        with patch("scripts.live_eval.task_snapshot.sys.platform", "win32"):
            with self.assertRaisesRegex(
                TaskSnapshotError, "^task_source_platform_unsupported$"
            ):
                _require_supported_platform()

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name).resolve()
        repo = root / "repo"
        repo.mkdir(mode=0o700)
        link = root / "repo-link"
        link.symlink_to(repo)
        for candidate in (Path("relative"), link):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(
                TaskSnapshotError, "^task_source_root_invalid$"
            ):
                _validate_physical_root(candidate)

    def test_pathlike_failure_is_fixed_and_has_no_exception_chain(self):
        class RaisingPath:
            def __fspath__(self):
                raise RuntimeError("private-path-sentinel")

        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_source_spec_invalid$"
        ) as caught:
            TaskSourceSpec(
                input_digest="sha256:" + "1" * 64,
                task_id="task-1",
                repository_root=RaisingPath(),
                commit_oid="a" * 40,
                provisioning_class="operator_owned_trusted_git_local_clone",
                operator_attested=True,
                local_clone_policy="remote_or_no_local_or_no_hardlinks",
            )
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)


class TaskSnapshotControlTests(RepositoryFixture, unittest.TestCase):
    def test_config_parser_accepts_closed_sha1_profile(self):
        raw_digest = "sha256:" + "a" * 64
        output = (
            b"core.repositoryformatversion\n0\0"
            b"core.filemode\ntrue\0"
            b"core.bare\nfalse\0"
            b"core.logallrefupdates\ntrue\0"
            b"remote.origin.url\nhttps://example.invalid/repo.git\0"
            b"remote.origin.fetch\n+refs/heads/*:refs/remotes/origin/*\0"
        )

        digest = _parse_config_output(output, raw_digest, "sha1")

        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        _parse_packed_refs(
            (
                "# pack-refs with: peeled sorted \n"
                + "a" * 40
                + " refs/heads/main\n"
                + "^"
                + "b" * 40
                + "\n"
            ).encode("ascii"),
            40,
        )
        for packed in (
            b"# pack-refs with: peeled\n",
            ("a" * 40 + " refs/replace/sentinel\n").encode("ascii"),
            ("a" * 40 + " refs/heads/main").encode("ascii"),
        ):
            with self.subTest(packed=packed), self.assertRaisesRegex(
                TaskSnapshotError, "^task_source_control_invalid$"
            ):
                _parse_packed_refs(packed, 40)

    def test_config_parser_rejects_authority_and_framing_attacks(self):
        raw_digest = "sha256:" + "a" * 64
        base = (
            b"core.repositoryformatversion\n0\0"
            b"core.bare\nfalse\0"
        )
        attacks = (
            base[:-1],
            base + b"core.bare\nfalse\0",
            base + b"include.path\n/tmp/sentinel\0",
            base + b"core.fsmonitor\n/tmp/sentinel\0",
            base + b"filter.lfs.clean\nsentinel\0",
            base + b"remote.origin.promisor\ntrue\0",
            base
            + "remote.origin.url\nhttps://example.invalid/\u0085secret\0".encode(
                "utf-8"
            ),
            b"core.repositoryformatversion\n1\0core.bare\nfalse\0",
            base + b"bad\nvalue\nextra\0",
        )
        for payload in attacks:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                TaskSnapshotError, "^task_source_config_invalid$"
            ) as caught:
                _parse_config_output(payload, raw_digest, "sha1")
            self.assertIsNone(caught.exception.__context__)
            self.assertIsNone(caught.exception.__cause__)

    def test_promisor_prescan_has_an_independent_inclusive_entry_cap(self):
        repo, oid = self.make_repository()
        pack = repo / ".git" / "objects" / "pack"
        pack.mkdir(exist_ok=True)
        cap = 20
        for index in range(cap):
            (pack / "sentinel-{:02d}".format(index)).write_bytes(b"x")
        source = self.source_for(repo, oid)
        policy = TaskSnapshotPolicy(max_object_entries=cap)

        _capture_filesystem(source, policy, "sha1")
        (pack / "sentinel-over").write_bytes(b"x")

        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_source_control_invalid$"
        ):
            _capture_filesystem(source, policy, "sha1")

    def test_controls_bind_config_and_reject_indirection(self):
        policy = TaskSnapshotPolicy()
        attacks = (
            (".git/commondir", b"../outside\n"),
            (".git/config.worktree", b"[core]\n"),
            (".git/objects/info/alternates", b"/tmp/sentinel\n"),
            (".git/hooks/post-checkout", b"#!/bin/sh\nexit 1\n"),
        )
        for relative_path, payload in attacks:
            with self.subTest(relative_path=relative_path):
                repo, oid = self.make_repository()
                target = repo / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                with self.assertRaisesRegex(
                    TaskSnapshotError, "^task_source_control_invalid$"
                ):
                    _capture_filesystem(
                        self.source_for(repo, oid), policy, "sha1"
                    )


class TaskSnapshotTopologyTests(RepositoryFixture, unittest.TestCase):
    def test_valid_object_topology_is_canonical_and_bounded(self):
        repo, oid = self.make_repository()
        objects = repo / ".git" / "objects"
        info = objects / "info"
        graphs = info / "commit-graphs"
        pack = objects / "pack"
        graphs.mkdir(exist_ok=True)
        for relative in (
            "info/packs",
            "info/commit-graph",
            "info/commit-graphs/commit-graph-chain",
            "info/commit-graphs/graph-" + "b" * 40 + ".graph",
            "pack/pack-" + "a" * 40 + ".pack",
            "pack/pack-" + "a" * 40 + ".idx",
            "pack/pack-" + "a" * 40 + ".rev",
            "pack/pack-" + "a" * 40 + ".bitmap",
            "pack/pack-" + "a" * 40 + ".mtimes",
            "pack/pack-" + "a" * 40 + ".keep",
            "pack/multi-pack-index",
            "pack/multi-pack-index-" + "c" * 40 + ".bitmap",
        ):
            target = objects / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
        source = self.source_for(repo, oid)
        policy = TaskSnapshotPolicy()

        seal = _capture_object_topology(source, policy, "sha1")

        self.assertRegex(
            seal.object_topology_digest, r"^sha256:[0-9a-f]{64}$"
        )
        self.assertGreaterEqual(seal.entry_count, seal.file_count + 1)
        self.assertGreater(seal.file_count, 0)
        self.assertGreater(seal.total_bytes, 0)
        exact_policy = TaskSnapshotPolicy(
            max_object_entries=seal.entry_count,
            max_files=seal.file_count,
            max_object_store_bytes=seal.total_bytes,
        )
        self.assertEqual(
            _capture_object_topology(source, exact_policy, "sha1"), seal
        )
        for values in (
            {"max_object_entries": seal.entry_count - 1},
            {"max_files": seal.file_count - 1},
            {"max_object_store_bytes": seal.total_bytes - 1},
        ):
            with self.subTest(values=values), self.assertRaisesRegex(
                TaskSnapshotError, "^task_source_topology_invalid$"
            ):
                _capture_object_topology(
                    source, TaskSnapshotPolicy(**values), "sha1"
                )

    def test_object_topology_rejects_unknown_pairing_and_file_attacks(self):
        attacks = (
            "unknown",
            "unpaired",
            "symlink",
            "hardlink",
            "fifo",
            "socket",
            "uppercase",
            "promisor",
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                repo, oid = self.make_repository()
                objects = repo / ".git" / "objects"
                pack = objects / "pack"
                pack.mkdir(exist_ok=True)
                if attack == "unknown":
                    (objects / "unknown").write_bytes(b"x")
                elif attack == "unpaired":
                    (pack / ("pack-" + "a" * 40 + ".pack")).write_bytes(b"x")
                elif attack == "symlink":
                    (pack / "sentinel").symlink_to(repo / "tracked.txt")
                elif attack == "hardlink":
                    original = next(
                        item
                        for directory in objects.iterdir()
                        if len(directory.name) == 2
                        for item in directory.iterdir()
                    )
                    os.link(original, pack / "sentinel")
                elif attack == "fifo":
                    os.mkfifo(pack / "sentinel")
                elif attack == "socket":
                    unix_socket = socket.socket(socket.AF_UNIX)
                    self.addCleanup(unix_socket.close)
                    unix_socket.bind(str(pack / "sentinel"))
                elif attack == "uppercase":
                    (objects / "AA").mkdir()
                else:
                    (pack / ("pack-" + "a" * 40 + ".promisor")).write_bytes(
                        b"sentinel"
                    )
                with self.assertRaisesRegex(
                    TaskSnapshotError, "^task_source_topology_invalid$"
                ):
                    _capture_object_topology(
                        self.source_for(repo, oid),
                        TaskSnapshotPolicy(),
                        "sha1",
                    )

    def test_versioned_evidence_digests_are_path_private(self):
        policy = TaskSnapshotPolicy()
        first = _process_policy_digest(policy)
        second = _process_policy_digest(TaskSnapshotPolicy())
        source_identity = _source_identity_digest(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64
        )

        self.assertEqual(first, second)
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(source_identity, r"^sha256:[0-9a-f]{64}$")

    def test_child_descriptor_faults_close_once_without_fd_leak(self):
        repo, oid = self.make_repository()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        real_open = os.open
        real_fstat = os.fstat
        real_close = os.close
        for fault in ("fstat", "identity"):
            with self.subTest(fault=fault):
                parent_fd = real_open(str(repo), flags)
                acquired = []
                closed = []

                def tracking_open(path, open_flags, *args, **kwargs):
                    descriptor = real_open(path, open_flags, *args, **kwargs)
                    if path == ".git":
                        acquired.append(descriptor)
                    return descriptor

                def failing_fstat(descriptor):
                    if fault == "fstat" and acquired and descriptor == acquired[0]:
                        raise OSError(errno.EIO, "fd-sentinel")
                    return real_fstat(descriptor)

                def tracking_close(descriptor):
                    if acquired and descriptor == acquired[0]:
                        closed.append(descriptor)
                    return real_close(descriptor)

                identity_patch = (
                    patch(
                        "scripts.live_eval.task_snapshot._same_identity",
                        side_effect=ValueError("identity-sentinel"),
                    )
                    if fault == "identity"
                    else patch(
                        "scripts.live_eval.task_snapshot._same_identity",
                        wraps=task_snapshot_module._same_identity,
                    )
                )
                try:
                    with patch(
                        "scripts.live_eval.task_snapshot.os.open",
                        side_effect=tracking_open,
                    ), patch(
                        "scripts.live_eval.task_snapshot.os.fstat",
                        side_effect=failing_fstat,
                    ), patch(
                        "scripts.live_eval.task_snapshot.os.close",
                        side_effect=tracking_close,
                    ), identity_patch, self.assertRaises(TaskSnapshotError):
                        _open_child_directory(
                            parent_fd,
                            ".git",
                            real_fstat(parent_fd).st_dev,
                            "task_source_git_dir_invalid",
                        )
                    self.assertEqual(len(acquired), 1)
                    self.assertEqual(closed, acquired)
                    with self.assertRaises(OSError) as caught:
                        real_fstat(acquired[0])
                    self.assertEqual(caught.exception.errno, errno.EBADF)
                finally:
                    real_close(parent_fd)

    def test_scandir_transfer_fault_closes_new_child_descriptor_once(self):
        repo, oid = self.make_repository()
        real_scandir = os.scandir
        real_fstat = os.fstat
        real_close = os.close
        calls = []
        failed_fd = []
        closed = []

        def failing_scandir(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                failed_fd.append(descriptor)
                raise OSError(errno.EIO, "scandir-sentinel")
            return real_scandir(descriptor)

        def tracking_close(descriptor):
            if failed_fd and descriptor == failed_fd[0]:
                closed.append(descriptor)
            return real_close(descriptor)

        with patch(
            "scripts.live_eval.task_snapshot.os.scandir",
            side_effect=failing_scandir,
        ), patch(
            "scripts.live_eval.task_snapshot.os.close",
            side_effect=tracking_close,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_source_topology_invalid$"
        ):
            _capture_object_topology(
                self.source_for(repo, oid), TaskSnapshotPolicy(), "sha1"
            )

        self.assertEqual(len(failed_fd), 1)
        self.assertEqual(closed, failed_fd)
        with self.assertRaises(OSError) as caught:
            real_fstat(failed_fd[0])
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_root_transfer_close_error_preserves_reused_descriptor(self):
        repo, oid = self.make_repository()
        real_close = os.close
        real_open = os.open
        real_fstat = os.fstat
        replacement = []
        triggered = {"value": False}
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC

        def close_then_reuse(descriptor):
            if not triggered["value"]:
                triggered["value"] = True
                real_close(descriptor)
                reused = real_open(str(repo.parent), flags)
                self.assertEqual(reused, descriptor)
                replacement.append(reused)
                raise OSError(errno.EINTR, "close-reuse-sentinel")
            return real_close(descriptor)

        with patch(
            "scripts.live_eval.task_snapshot.os.close",
            side_effect=close_then_reuse,
        ), self.assertRaises(TaskSnapshotError):
            _open_root_descriptor(repo)

        self.assertEqual(len(replacement), 1)
        real_fstat(replacement[0])
        real_close(replacement[0])

    def test_frame_close_error_preserves_reused_descriptor(self):
        repo, oid = self.make_repository()
        real_close = os.close
        real_open = os.open
        real_fstat = os.fstat
        real_scandir = os.scandir
        scanned = set()
        replacement = []
        triggered = {"value": False}
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC

        def tracking_scandir(descriptor):
            scanned.add(descriptor)
            return real_scandir(descriptor)

        def close_then_reuse(descriptor):
            if descriptor in scanned and not triggered["value"]:
                triggered["value"] = True
                real_close(descriptor)
                reused = real_open(str(repo.parent), flags)
                self.assertEqual(reused, descriptor)
                replacement.append(reused)
                raise OSError(errno.EINTR, "frame-close-reuse-sentinel")
            return real_close(descriptor)

        with patch(
            "scripts.live_eval.task_snapshot.os.scandir",
            side_effect=tracking_scandir,
        ), patch(
            "scripts.live_eval.task_snapshot.os.close",
            side_effect=close_then_reuse,
        ), self.assertRaises(TaskSnapshotError):
            _capture_object_topology(
                self.source_for(repo, oid), TaskSnapshotPolicy(), "sha1"
            )

        self.assertEqual(len(replacement), 1)
        real_fstat(replacement[0])
        real_close(replacement[0])

    def test_sibling_churn_inside_descriptor_window_is_stable(self):
        repo, oid = self.make_repository()
        real_open = os.open
        churned = {"value": False}

        def opening_with_sibling_churn(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == repo.name and not churned["value"]:
                (repo.parent / "descriptor-window-sibling").write_bytes(b"x")
                churned["value"] = True
            return descriptor

        with patch(
            "scripts.live_eval.task_snapshot.os.open",
            side_effect=opening_with_sibling_churn,
        ):
            descriptor, metadata, ancestors = _open_root_descriptor(repo)
        try:
            self.assertTrue(churned["value"])
            self.assertEqual(metadata.st_ino, os.stat(repo).st_ino)
            self.assertTrue(ancestors)
        finally:
            os.close(descriptor)


class TaskSnapshotGitAdapterTests(RepositoryFixture, unittest.TestCase):
    def test_real_config_and_storage_operations_are_bounded(self):
        repo, oid = self.make_repository()
        policy = TaskSnapshotPolicy()
        git_dir = repo / ".git"
        config = git_dir / "config"

        config_output = _run_git(
            git_dir, config, policy, "config"
        )
        storage_output = _run_git(
            git_dir, config, policy, "storage-format"
        )

        self.assertTrue(config_output.endswith(b"\0"))
        self.assertEqual(storage_output, b"sha1\n")
        exact = TaskSnapshotPolicy(
            max_git_stdout_bytes=len(config_output),
            max_git_stderr_bytes=min(64, len(config_output)),
            max_config_bytes=len(config_output),
        )
        self.assertEqual(
            _run_git(git_dir, config, exact, "config"), config_output
        )
        over = TaskSnapshotPolicy(
            max_git_stdout_bytes=len(config_output) - 1,
            max_git_stderr_bytes=min(64, len(config_output) - 1),
            max_config_bytes=len(config_output) - 1,
        )
        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_output_limit$"
        ):
            _run_git(git_dir, config, over, "config")

    def test_exact_process_contract_and_replacement_environment(self):
        repo, oid = self.make_repository()

        class FakeProcess:
            pid = 987654
            returncode = 0

            def __init__(self):
                self.stdout = io.BytesIO(b"sha1\n")
                self.stderr = io.BytesIO(b"")

            def poll(self):
                return 0

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

        fake = FakeProcess()
        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=fake,
        ) as popen, patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=ProcessLookupError,
        ):
            output = _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(),
                "storage-format",
            )

        self.assertEqual(output, b"sha1\n")
        argv = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(argv[0], "git")
        self.assertEqual(argv[-2:], ("rev-parse", "--show-object-format=storage"))
        self.assertEqual(options["cwd"], "/")
        self.assertFalse(options["shell"])
        self.assertTrue(options["close_fds"])
        self.assertTrue(options["start_new_session"])
        self.assertEqual(
            set(options["env"]),
            {
                "GIT_ATTR_NOSYSTEM",
                "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_NO_LAZY_FETCH",
                "GIT_NO_REPLACE_OBJECTS",
                "GIT_OPTIONAL_LOCKS",
                "GIT_TERMINAL_PROMPT",
                "LANG",
                "LC_ALL",
                "PATH",
            },
        )

    def test_operation_allowlist_and_errors_are_sanitized(self):
        repo, oid = self.make_repository()
        attacks = (
            ("status", None),
            ("verify-commit", "HEAD"),
            ("cat-blob", "../sentinel"),
        )
        for operation, value in attacks:
            with self.subTest(operation=operation), self.assertRaisesRegex(
                TaskSnapshotError, "^task_git_operation_invalid$"
            ) as caught:
                _run_git(
                    repo / ".git",
                    repo / ".git" / "config",
                    TaskSnapshotPolicy(),
                    operation,
                    value,
                )
            self.assertIsNone(caught.exception.__context__)
            self.assertIsNone(caught.exception.__cause__)

    def test_timeout_cleans_up_and_preserves_fixed_classification(self):
        repo, oid = self.make_repository()

        class FakeProcess:
            pid = 987654
            returncode = None

            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")

            def poll(self):
                return None

            def wait(self, timeout=None):
                self.returncode = -15
                return self.returncode

        fake = FakeProcess()
        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=fake,
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=ProcessLookupError,
        ), patch(
            "scripts.live_eval.task_snapshot.time.monotonic",
            side_effect=(0.0, 2.0, 2.0, 2.0),
        ):
            with self.assertRaisesRegex(
                TaskSnapshotError, "^task_git_timeout$"
            ):
                _run_git(
                    repo / ".git",
                    repo / ".git" / "config",
                    TaskSnapshotPolicy(git_timeout_seconds=1),
                    "storage-format",
                )

    def test_spawn_failure_is_fixed_and_has_no_exception_chain(self):
        repo, oid = self.make_repository()
        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            side_effect=OSError("spawn-private-sentinel"),
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_spawn_failed$"
        ) as caught:
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(),
                "storage-format",
            )
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

        class RaisingConfigPath:
            def __fspath__(self):
                raise RuntimeError("config-path-private-sentinel")

        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_operation_invalid$"
        ) as caught:
            _run_git(
                repo / ".git",
                RaisingConfigPath(),
                TaskSnapshotPolicy(),
                "config",
            )
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_partial_reader_start_failure_cleans_only_started_thread(self):
        repo, oid = self.make_repository()
        signals = []
        threads = []

        class FakeProcess:
            pid = 987654
            returncode = None

            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = -15
                return self.returncode

        class FakeThread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args
                self.started = False
                self.joined = False
                threads.append(self)

            def start(self):
                if len(threads) == 2:
                    raise RuntimeError("thread-start-sentinel")
                self.started = True
                self.target(*self.args)

            def join(self, timeout=None):
                if not self.started:
                    raise RuntimeError("joined-unstarted-sentinel")
                self.joined = True

            def is_alive(self):
                return False

        group_alive = {"value": True}

        def kill_group(process_group, selected_signal):
            signals.append(selected_signal)
            if selected_signal == 0:
                if group_alive["value"]:
                    return None
                raise ProcessLookupError
            if selected_signal == task_snapshot_module.signal.SIGTERM:
                group_alive["value"] = False

        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=FakeProcess(),
        ), patch(
            "scripts.live_eval.task_snapshot.threading.Thread",
            side_effect=FakeThread,
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=kill_group,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_failed$"
        ) as caught:
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(),
                "storage-format",
            )

        self.assertTrue(threads[0].joined)
        self.assertFalse(threads[1].joined)
        self.assertIn(task_snapshot_module.signal.SIGTERM, signals)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_poll_monitor_failure_runs_cleanup_and_is_fixed(self):
        repo, oid = self.make_repository()
        signals = []

        class StatefulProcess:
            pid = 987654
            returncode = None

            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")
                self.poll_calls = 0

            def poll(self):
                self.poll_calls += 1
                if self.poll_calls == 1:
                    raise RuntimeError("poll-private-sentinel")
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = -15
                return self.returncode

        process = StatefulProcess()
        group_alive = {"value": True}

        def kill_group(process_group, selected_signal):
            signals.append(selected_signal)
            if selected_signal == 0:
                if group_alive["value"]:
                    return None
                raise ProcessLookupError
            group_alive["value"] = False

        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=process,
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=kill_group,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_failed$"
        ) as caught:
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(),
                "storage-format",
            )

        self.assertIn(task_snapshot_module.signal.SIGTERM, signals)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_deadline_starts_before_popen(self):
        repo, oid = self.make_repository()
        reader_constructions = []

        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        class FakeProcess:
            pid = 987654
            returncode = 0

            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        clock = Clock()

        def delayed_spawn(*args, **kwargs):
            clock.value = 2.0
            return FakeProcess()

        def unexpected_reader(*args, **kwargs):
            reader_constructions.append((args, kwargs))
            raise AssertionError("reader-started-after-expired-spawn")

        with patch(
            "scripts.live_eval.task_snapshot.time.monotonic", clock
        ), patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            side_effect=delayed_spawn,
        ), patch(
            "scripts.live_eval.task_snapshot.threading.Thread",
            side_effect=unexpected_reader,
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=ProcessLookupError,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_timeout$"
        ):
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(git_timeout_seconds=1),
                "storage-format",
            )
        self.assertEqual(reader_constructions, [])

    def test_deadline_is_checked_after_each_reader_start(self):
        repo, oid = self.make_repository()
        threads = []

        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        class FakeProcess:
            pid = 987654
            returncode = 0

            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        clock = Clock()

        class FakeThread:
            def __init__(self, target, args, daemon):
                self.started = False
                self.joined = False
                threads.append(self)

            def start(self):
                self.started = True
                clock.value = 2.0

            def join(self, timeout=None):
                if not self.started:
                    raise RuntimeError("joined-unstarted-reader")
                self.joined = True

            def is_alive(self):
                return False

        with patch(
            "scripts.live_eval.task_snapshot.time.monotonic", clock
        ), patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=FakeProcess(),
        ), patch(
            "scripts.live_eval.task_snapshot.threading.Thread",
            side_effect=FakeThread,
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=ProcessLookupError,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_timeout$"
        ):
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(git_timeout_seconds=1),
                "storage-format",
            )

        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].joined)

    def test_cleanup_reaps_after_kill_despite_poll_and_wait_failures(self):
        signals = []

        class AdvancingClock:
            value = 0.0

            def __call__(self):
                self.value += 1.0
                return self.value

        class BrokenProcess:
            pid = 987654

            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")
                self.wait_calls = 0

            def poll(self):
                raise RuntimeError("persistent-poll-sentinel")

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("git", timeout)
                return -9

        process = BrokenProcess()
        group_alive = {"value": True}

        def kill_group(process_group, selected_signal):
            signals.append(selected_signal)
            if selected_signal == 0:
                if group_alive["value"]:
                    return None
                raise ProcessLookupError
            if selected_signal == task_snapshot_module.signal.SIGKILL:
                group_alive["value"] = False

        with patch(
            "scripts.live_eval.task_snapshot.time.monotonic",
            AdvancingClock(),
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=kill_group,
        ):
            result = _cleanup_process(process, process.pid, 0.01)

        self.assertFalse(result)
        self.assertIn(task_snapshot_module.signal.SIGKILL, signals)
        self.assertEqual(process.wait_calls, 2)

    def test_nominal_join_failure_triggers_cleanup_and_fixed_error(self):
        repo, oid = self.make_repository()
        process_waits = []

        class FakeProcess:
            pid = 987654
            returncode = 0

            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                process_waits.append(timeout)
                return self.returncode

        class JoinFailingThread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

            def join(self, timeout=None):
                raise RuntimeError("join-private-sentinel")

            def is_alive(self):
                return False

        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=FakeProcess(),
        ), patch(
            "scripts.live_eval.task_snapshot.threading.Thread",
            side_effect=JoinFailingThread,
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=ProcessLookupError,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_failed$"
        ) as caught:
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(),
                "storage-format",
            )

        self.assertGreaterEqual(len(process_waits), 2)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_nonzero_leader_cleans_open_pipe_descendants_immediately(self):
        repo, oid = self.make_repository()
        signals = []
        threads = []

        class FakeProcess:
            pid = 987654
            returncode = 7

            def __init__(self):
                self.stdout = io.BytesIO(b"still-open")
                self.stderr = io.BytesIO(b"still-open")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        class StalledReader:
            def __init__(self, target, args, daemon):
                self.started = False
                self.joined = False
                threads.append(self)

            def start(self):
                self.started = True

            def join(self, timeout=None):
                self.joined = True

            def is_alive(self):
                return False

        group_alive = {"value": True}

        def kill_group(process_group, selected_signal):
            signals.append(selected_signal)
            if selected_signal == 0:
                if group_alive["value"]:
                    return None
                raise ProcessLookupError
            group_alive["value"] = False

        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=FakeProcess(),
        ) as popen, patch(
            "scripts.live_eval.task_snapshot.threading.Thread",
            side_effect=StalledReader,
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=kill_group,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_failed$"
        ):
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(git_timeout_seconds=1),
                "storage-format",
            )

        process = popen.return_value
        self.assertIn(task_snapshot_module.signal.SIGTERM, signals)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertTrue(all(thread.joined for thread in threads))

    def test_simultaneous_pipe_caps_and_residual_group_fail_closed(self):
        repo, oid = self.make_repository()

        class TrackingBytesIO(io.BytesIO):
            consumed = None

            def close(self):
                self.consumed = self.tell()
                super().close()

        class FakeProcess:
            pid = 987654
            returncode = 0

            def __init__(self, stdout, stderr):
                self.stdout = TrackingBytesIO(stdout)
                self.stderr = TrackingBytesIO(stderr)

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        saturated = FakeProcess(b"12345", b"abcde")
        cap_policy = TaskSnapshotPolicy(
            max_git_stdout_bytes=4,
            max_git_stderr_bytes=4,
            max_config_bytes=4,
        )
        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=saturated,
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=ProcessLookupError,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_output_limit$"
        ):
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                cap_policy,
                "storage-format",
            )
        self.assertEqual(saturated.stdout.consumed, 5)
        self.assertEqual(saturated.stderr.consumed, 5)

        residual = FakeProcess(b"", b"")
        signals = []
        group_alive = {"value": True}

        def kill_group(process_group, selected_signal):
            signals.append(selected_signal)
            if selected_signal == 0:
                if group_alive["value"]:
                    return None
                raise ProcessLookupError
            group_alive["value"] = False

        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=residual,
        ), patch(
            "scripts.live_eval.task_snapshot.os.killpg",
            side_effect=kill_group,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_failed$"
        ):
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(),
                "storage-format",
            )
        self.assertIn(task_snapshot_module.signal.SIGTERM, signals)
        self.assertFalse(group_alive["value"])


class TaskSnapshotPreparationTests(RepositoryFixture, unittest.TestCase):
    def test_prepares_immutable_path_private_source_without_receipt(self):
        for object_format in ("sha1", "sha256"):
            with self.subTest(object_format=object_format):
                repo, oid = self.make_repository(object_format)
                prepared = prepare_task_source(
                    self.source_for(repo, oid), TaskSnapshotPolicy()
                )

                self.assertIs(type(prepared), PreparedTaskSource)
                self.assertEqual(prepared.git_dir, repo / ".git")
                self.assertEqual(prepared.object_format, object_format)
                self.assertRegex(
                    prepared.source_identity_digest,
                    r"^sha256:[0-9a-f]{64}$",
                )
                self.assertRegex(
                    prepared.local_config_digest, r"^sha256:[0-9a-f]{64}$"
                )
                self.assertIs(
                    type(prepared.object_topology), ObjectTopologySeal
                )
        self.assertNotIn(
            "experiment_receipt",
            Path(task_snapshot_module.__file__).read_text(encoding="utf-8"),
        )

    def test_preparation_uses_only_config_storage_config_order(self):
        repo, oid = self.make_repository()
        operations = []
        original = task_snapshot_module._run_git

        def recording_run(git_dir, config_path, policy, operation, value=None):
            operations.append((operation, value))
            return original(
                git_dir, config_path, policy, operation, value
            )

        with patch(
            "scripts.live_eval.task_snapshot._run_git",
            side_effect=recording_run,
        ):
            prepare_task_source(
                self.source_for(repo, oid), TaskSnapshotPolicy()
            )

        self.assertEqual(
            operations,
            [("config", None), ("storage-format", None), ("config", None)],
        )

    def test_sticky_ancestor_sibling_churn_does_not_change_source_seal(self):
        repo, oid = self.make_repository()
        source = self.source_for(repo, oid)
        original = task_snapshot_module._run_git
        mutated = {"value": False}

        def sibling_churn(git_dir, config_path, policy, operation, value=None):
            if not mutated["value"]:
                (repo.parent / "unrelated-sibling").write_bytes(b"x")
                mutated["value"] = True
            return original(git_dir, config_path, policy, operation, value)

        with patch(
            "scripts.live_eval.task_snapshot._run_git",
            side_effect=sibling_churn,
        ):
            prepared = prepare_task_source(source, TaskSnapshotPolicy())

        self.assertIs(type(prepared), PreparedTaskSource)
        ancestor_keys = {"kind", "mode", "dev", "ino", "uid", "gid"}
        self.assertTrue(
            all(
                set(record) == ancestor_keys
                for record in _validate_physical_root(repo)
            )
        )

    def test_seal_change_is_rejected_after_second_capture(self):
        repo, oid = self.make_repository()
        original = task_snapshot_module._capture_object_topology
        calls = []

        def changing_capture(source, policy, object_format):
            seal = original(source, policy, object_format)
            calls.append(seal)
            if len(calls) == 2:
                return replace(
                    seal,
                    object_topology_digest="sha256:" + "f" * 64,
                )
            return seal

        with patch(
            "scripts.live_eval.task_snapshot._capture_object_topology",
            side_effect=changing_capture,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_source_changed$"
        ):
            prepare_task_source(
                self.source_for(repo, oid), TaskSnapshotPolicy()
            )

    def test_forged_prepared_and_topology_values_fail_closed(self):
        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_source_topology_invalid$"
        ):
            ObjectTopologySeal(
                object_topology_digest="sha256:" + "a" * 64,
                entry_count=True,
                file_count=0,
                total_bytes=0,
            )


if __name__ == "__main__":
    unittest.main()

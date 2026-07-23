import os
from pathlib import Path
import subprocess
import tempfile
import errno
import hashlib
import io
import socket
from types import MappingProxyType
import unittest
from dataclasses import fields, replace
from unittest.mock import patch

import scripts.live_eval.task_snapshot as task_snapshot_module
from scripts.live_eval.task_snapshot import (
    ObjectTopologySeal,
    PreparedTaskSource,
    TaskTreeEntry,
    TaskSnapshotError,
    TaskSnapshotPolicy,
    TaskSourceSpec,
    _capture_filesystem,
    _capture_object_topology,
    _cleanup_process,
    _git_operation_tail,
    _open_root_descriptor,
    _parse_config_output,
    _parse_packed_refs,
    _process_policy_digest,
    _load_task_blobs,
    _parse_task_tree,
    _require_supported_platform,
    _run_git,
    _source_identity_digest,
    _task_entry_digest,
    _task_entry_document,
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

    def test_task7_policy_fields_have_exact_order_defaults_and_hard_caps(self):
        expected = (
            ("policy_version", "task-object-materializer-v1"),
            ("git_timeout_seconds", 15),
            ("git_termination_grace_milliseconds", 250),
            ("max_git_stdout_bytes", 16 * 1024 * 1024),
            ("max_git_stderr_bytes", 64 * 1024),
            ("capture_timeout_seconds", 60),
            ("max_config_bytes", 256 * 1024),
            ("max_packed_refs_bytes", 4 * 1024 * 1024),
            ("max_object_entries", 200000),
            ("max_object_store_bytes", 2 * 1024 * 1024 * 1024),
            ("max_object_depth", 3),
            ("max_component_bytes", 255),
            ("max_relative_path_bytes", 4096),
            ("max_tree_entries", 100000),
            ("max_tree_depth", 64),
            ("max_files", 10000),
            ("max_unique_blobs", 256),
            ("max_file_bytes", 4 * 1024 * 1024),
            ("max_total_bytes", 64 * 1024 * 1024),
        )
        policy = TaskSnapshotPolicy()

        self.assertEqual(
            tuple((item.name, getattr(policy, item.name)) for item in fields(policy)),
            expected,
        )
        for name, ceiling in (
            ("capture_timeout_seconds", 60),
            ("max_tree_entries", 100000),
            ("max_tree_depth", 64),
            ("max_unique_blobs", 256),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(TaskSnapshotPolicy(**{name: ceiling}), name),
                    ceiling,
                )
                self.assertEqual(
                    getattr(TaskSnapshotPolicy(**{name: 1}), name),
                    1,
                )
                for invalid in (True, 0, ceiling + 1):
                    with self.assertRaisesRegex(
                        TaskSnapshotError,
                        "^task_snapshot_policy_invalid$",
                    ):
                        TaskSnapshotPolicy(**{name: invalid})

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

    def test_nonfinal_private_tmp_sibling_churn_is_stable(self):
        private_temp_root = Path("/private/tmp")
        physical_temp_root = (
            private_temp_root
            if private_temp_root.is_dir()
            else Path(tempfile.gettempdir()).resolve()
        )
        temporary_directory = tempfile.TemporaryDirectory(
            dir=str(physical_temp_root)
        )
        self.addCleanup(temporary_directory.cleanup)
        repo = Path(temporary_directory.name).resolve() / "repo"
        repo.mkdir(mode=0o700)
        real_open = os.open
        sibling_directories = []
        descriptor = -1

        def opening_with_temp_sibling_churn(path, flags, *args, **kwargs):
            opened_descriptor = real_open(path, flags, *args, **kwargs)
            if (
                path == physical_temp_root.name
                and not sibling_directories
            ):
                sibling_directories.append(
                    Path(
                        tempfile.mkdtemp(
                            prefix="task-snapshot-sibling-",
                            dir=str(physical_temp_root),
                        )
                    )
                )
            return opened_descriptor

        try:
            try:
                with patch(
                    "scripts.live_eval.task_snapshot.os.open",
                    side_effect=opening_with_temp_sibling_churn,
                ):
                    descriptor, metadata, ancestors = (
                        _open_root_descriptor(repo)
                    )
            except TaskSnapshotError as error:
                self.fail(
                    "non-final ancestor sibling churn was rejected: "
                    + str(error)
                )
            self.assertEqual(len(sibling_directories), 1)
            self.assertEqual(metadata.st_ino, os.stat(repo).st_ino)
            self.assertTrue(ancestors)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            for sibling in sibling_directories:
                sibling.rmdir()


class TaskSnapshotGitAdapterTests(RepositoryFixture, unittest.TestCase):
    def test_task7_process_policy_and_formatted_tree_tail_are_exact(self):
        policy = TaskSnapshotPolicy(
            git_timeout_seconds=3,
            git_termination_grace_milliseconds=10,
            max_git_stdout_bytes=4096,
            max_git_stderr_bytes=512,
            capture_timeout_seconds=7,
            max_config_bytes=1024,
        )
        expected_digest = "sha256:" + "d" * 64
        expected_tree_tail = (
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "--format=%(objectmode)%x09%(objecttype)%x09"
            "%(objectname)%x09%(objectsize)%x09%(path)",
            "a" * 40,
        )

        self.assertEqual(
            _git_operation_tail(
                "ls-tree", Path("/verified/config"), "a" * 40
            ),
            expected_tree_tail,
        )
        with patch(
            "scripts.live_eval.task_snapshot._digest",
            return_value=expected_digest,
        ) as digest:
            self.assertEqual(_process_policy_digest(policy), expected_digest)

        document = digest.call_args.args[0]
        self.assertEqual(
            document,
            {
                "argv_prefix": [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.attributesFile=<null-device>",
                    "-c",
                    "core.excludesFile=<null-device>",
                    "-c",
                    "core.hooksPath=<null-device>",
                    "-c",
                    "submodule.recurse=false",
                    "--git-dir=<verified-git-dir>",
                ],
                "capture_deadline": {
                    "capture_timeout_seconds": 7,
                    "clock": "time.monotonic",
                    "effective_process_deadline":
                        "earliest-of-capture-and-operation",
                    "equal_expiry_classification":
                        "task_capture_timeout",
                    "scope": "fresh-F0-through-source-trust-receipt",
                },
                "close_fds": True,
                "cwd": "/",
                "document_type": "task-git-process-policy-v1",
                "environment": {
                    "GIT_ATTR_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "<null-device>",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_NO_LAZY_FETCH": "1",
                    "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_TERMINAL_PROMPT": "0",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "<os.defpath>",
                },
                "limits": {
                    "git_termination_grace_milliseconds": 10,
                    "git_timeout_seconds": 3,
                    "max_git_stderr_bytes": 512,
                    "max_git_stdout_bytes": 4096,
                },
                "operation_templates": [
                    [
                        "config",
                        "--file=<verified-config>",
                        "--no-includes",
                        "--null",
                        "--list",
                    ],
                    ["rev-parse", "--show-object-format=storage"],
                    [
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        "<validated-full-commit-oid>^{commit}",
                    ],
                    [
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        "<validated-full-commit-oid>^{tree}",
                    ],
                    [
                        "ls-tree",
                        "-r",
                        "-z",
                        "--full-tree",
                        "--format=%(objectmode)%x09%(objecttype)%x09"
                        "%(objectname)%x09%(objectsize)%x09%(path)",
                        "<validated-full-tree-oid>",
                    ],
                    [
                        "cat-file",
                        "blob",
                        "<validated-full-blob-oid>",
                    ],
                ],
                "output_policy": {
                    "cap_is_inclusive": True,
                    "cap_plus_one_action":
                        "terminate-process-group",
                    "cat_file_blob_stdout_cap":
                        "validated-declared-blob-size",
                    "generic_stdout_cap": "max_git_stdout_bytes",
                    "successful_stderr": "empty",
                },
                "schema_version": 1,
                "shell": False,
                "start_new_session": True,
                "stdin": "DEVNULL",
                "termination": [
                    "concurrent-bounded-drain",
                    "monotonic-deadline",
                    "TERM",
                    "bounded-grace",
                    "KILL",
                    "reap",
                    "close-pipes",
                    "verify-group-absent",
                ],
            },
        )

    def test_success_with_stderr_is_rejected(self):
        repo, oid = self.make_repository()

        class FakeProcess:
            pid = 987654
            returncode = 0

            def __init__(self):
                self.stdout = io.BytesIO(b"sha1\n")
                self.stderr = io.BytesIO(b"warning-private-sentinel\n")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        with patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            return_value=FakeProcess(),
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

        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_blob_dynamic_stdout_cap_accepts_zero_and_exact_then_rejects_plus_one(
        self,
    ):
        repo, oid = self.make_repository()

        class FakeProcess:
            pid = 987654
            returncode = 0

            def __init__(self, output):
                self.stdout = io.BytesIO(output)
                self.stderr = io.BytesIO(b"")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        def run_blob(output, stdout_limit):
            with patch(
                "scripts.live_eval.task_snapshot.subprocess.Popen",
                return_value=FakeProcess(output),
            ), patch(
                "scripts.live_eval.task_snapshot.os.killpg",
                side_effect=ProcessLookupError,
            ):
                return _run_git(
                    repo / ".git",
                    repo / ".git" / "config",
                    TaskSnapshotPolicy(),
                    "cat-blob",
                    "a" * 40,
                    stdout_limit=stdout_limit,
                )

        self.assertEqual(run_blob(b"", 0), b"")
        self.assertEqual(run_blob(b"blob", 4), b"blob")
        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_blob_invalid$"
        ) as caught:
            run_blob(b"x", 0)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_capture_deadline_precedes_or_ties_operation_deadline(self):
        repo, oid = self.make_repository()

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

        cases = (
            (1, 1.0, 2.0, "task_capture_timeout"),
            (5, 1.0, 2.0, "task_capture_timeout"),
            (1, 5.0, 2.0, "task_git_timeout"),
            (1, 5.0, 6.0, "task_capture_timeout"),
        )
        for (
            operation_timeout,
            capture_deadline,
            observed_at,
            expected,
        ) in cases:
            with self.subTest(
                operation_timeout=operation_timeout,
                capture_deadline=capture_deadline,
                observed_at=observed_at,
            ):
                clock = Clock()

                def delayed_spawn(*args, **kwargs):
                    clock.value = observed_at
                    return FakeProcess()

                with patch(
                    "scripts.live_eval.task_snapshot.time.monotonic", clock
                ), patch(
                    "scripts.live_eval.task_snapshot.subprocess.Popen",
                    side_effect=delayed_spawn,
                ), patch(
                    "scripts.live_eval.task_snapshot.os.killpg",
                    side_effect=ProcessLookupError,
                ), self.assertRaisesRegex(
                    TaskSnapshotError, "^" + expected + "$"
                ):
                    _run_git(
                        repo / ".git",
                        repo / ".git" / "config",
                        TaskSnapshotPolicy(
                            git_timeout_seconds=operation_timeout
                        ),
                        "storage-format",
                        capture_deadline=capture_deadline,
                    )

    def test_expired_capture_deadline_prevents_spawn(self):
        repo, oid = self.make_repository()
        with patch(
            "scripts.live_eval.task_snapshot.time.monotonic",
            side_effect=(2.0, 2.0),
        ), patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
        ) as popen, self.assertRaisesRegex(
            TaskSnapshotError, "^task_capture_timeout$"
        ) as caught:
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(git_timeout_seconds=5),
                "storage-format",
                capture_deadline=1.0,
            )

        popen.assert_not_called()
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_raising_spawn_rechecks_capture_deadline(self):
        repo, oid = self.make_repository()

        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        cases = (
            (5, 2.0, 1.0, "task_capture_timeout"),
            (5, 2.0, 3.0, "task_git_spawn_failed"),
            (1, 2.0, 5.0, "task_git_timeout"),
        )
        for (
            operation_timeout,
            observed_at,
            capture_deadline,
            expected,
        ) in cases:
            with self.subTest(
                operation_timeout=operation_timeout,
                observed_at=observed_at,
                capture_deadline=capture_deadline,
            ):
                clock = Clock()

                def delayed_failure(*args, **kwargs):
                    clock.value = observed_at
                    raise OSError("spawn-private-sentinel")

                with patch(
                    "scripts.live_eval.task_snapshot.time.monotonic",
                    clock,
                ), patch(
                    "scripts.live_eval.task_snapshot.subprocess.Popen",
                    side_effect=delayed_failure,
                ) as popen, self.assertRaisesRegex(
                    TaskSnapshotError, "^" + expected + "$"
                ) as caught:
                    _run_git(
                        repo / ".git",
                        repo / ".git" / "config",
                        TaskSnapshotPolicy(
                            git_timeout_seconds=operation_timeout
                        ),
                        "storage-format",
                        capture_deadline=capture_deadline,
                    )

                self.assertEqual(popen.call_count, 1)
                self.assertIsNone(caught.exception.__context__)
                self.assertIsNone(caught.exception.__cause__)

    def test_blob_overflow_precedes_simultaneous_stderr_overflow(self):
        repo, oid = self.make_repository()

        class FakeProcess:
            pid = 987654
            returncode = 0

            def __init__(self):
                self.stdout = io.BytesIO(b"x")
                self.stderr = io.BytesIO(b"yz")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        class SynchronousThread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return False

        def run(cleanup_result=True):
            with patch(
                "scripts.live_eval.task_snapshot.subprocess.Popen",
                return_value=FakeProcess(),
            ), patch(
                "scripts.live_eval.task_snapshot.threading.Thread",
                side_effect=SynchronousThread,
            ), patch(
                "scripts.live_eval.task_snapshot.os.killpg",
                side_effect=ProcessLookupError,
            ), patch(
                "scripts.live_eval.task_snapshot._cleanup_git_failure",
                return_value=cleanup_result,
            ):
                return _run_git(
                    repo / ".git",
                    repo / ".git" / "config",
                    TaskSnapshotPolicy(max_git_stderr_bytes=1),
                    "cat-blob",
                    "a" * 40,
                    stdout_limit=0,
                )

        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_blob_invalid$"
        ):
            run()
        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_failed$"
        ):
            run(cleanup_result=False)

    def test_capture_timeout_cleanup_failure_overrides_classification(self):
        repo, oid = self.make_repository()

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

        with patch(
            "scripts.live_eval.task_snapshot.time.monotonic", clock
        ), patch(
            "scripts.live_eval.task_snapshot.subprocess.Popen",
            side_effect=delayed_spawn,
        ), patch(
            "scripts.live_eval.task_snapshot._cleanup_git_failure",
            return_value=False,
        ), self.assertRaisesRegex(
            TaskSnapshotError, "^task_git_failed$"
        ):
            _run_git(
                repo / ".git",
                repo / ".git" / "config",
                TaskSnapshotPolicy(git_timeout_seconds=5),
                "storage-format",
                capture_deadline=1.0,
            )

    def test_real_config_and_storage_operations_accept_inclusive_caps(self):
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


class TaskSnapshotTreeEvidenceTests(RepositoryFixture, unittest.TestCase):
    @staticmethod
    def blob_oid(content, object_format):
        framed = (
            b"blob "
            + str(len(content)).encode("ascii")
            + b"\0"
            + content
        )
        algorithm = hashlib.sha1 if object_format == "sha1" else hashlib.sha256
        return algorithm(framed).hexdigest()

    @staticmethod
    def tree_record(path, oid, size, mode="100644"):
        return (
            mode.encode("ascii")
            + b"\tblob\t"
            + oid.encode("ascii")
            + b"\t"
            + str(size).encode("ascii")
            + b"\t"
            + path.encode("utf-8")
            + b"\0"
        )

    def test_empty_and_sha_variants_build_sorted_immutable_evidence(self):
        policy = TaskSnapshotPolicy()
        empty = _parse_task_tree(b"", "sha1", policy)
        self.assertEqual(empty.file_count, 0)
        self.assertEqual(empty.directory_count, 1)
        self.assertEqual(empty.tree_entry_count, 1)
        self.assertEqual(empty.total_bytes, 0)
        self.assertEqual(empty.unique_blob_count, 0)
        self.assertEqual(empty.unique_blob_bytes, 0)

        for object_format in ("sha1", "sha256"):
            with self.subTest(object_format=object_format):
                first = b"same"
                second = b"other"
                first_oid = self.blob_oid(first, object_format)
                second_oid = self.blob_oid(second, object_format)
                output = b"".join(
                    (
                        self.tree_record(
                            "z.sh", first_oid, len(first), "100755"
                        ),
                        self.tree_record(
                            "a.txt", second_oid, len(second)
                        ),
                        self.tree_record(
                            "nested/repeat.txt", first_oid, len(first)
                        ),
                    )
                )
                parsed = _parse_task_tree(output, object_format, policy)
                calls = []
                contents = {first_oid: first, second_oid: second}

                def run_git(
                    git_dir,
                    config_path,
                    selected_policy,
                    operation,
                    value=None,
                    *,
                    capture_deadline=None,
                    stdout_limit=None
                ):
                    calls.append((operation, value, stdout_limit))
                    return contents[value]

                with patch(
                    "scripts.live_eval.task_snapshot._run_git",
                    side_effect=run_git,
                ):
                    entries, blobs = _load_task_blobs(
                        Path("/repo/.git"),
                        Path("/repo/.git/config"),
                        parsed,
                        policy,
                        capture_deadline=10.0,
                    )

                self.assertEqual(
                    [entry.path for entry in entries],
                    ["a.txt", "nested/repeat.txt", "z.sh"],
                )
                self.assertEqual(entries[-1].git_mode, "100755")
                self.assertEqual(type(blobs), MappingProxyType)
                self.assertEqual(
                    list(blobs),
                    sorted((first_oid, second_oid), key=lambda item: item.encode("ascii")),
                )
                self.assertEqual(
                    calls,
                    [
                        ("cat-blob", oid, len(contents[oid]))
                        for oid in sorted(
                            contents, key=lambda item: item.encode("ascii")
                        )
                    ],
                )
                self.assertEqual(parsed.file_count, 3)
                self.assertEqual(parsed.directory_count, 2)
                self.assertEqual(parsed.total_bytes, 13)
                self.assertEqual(parsed.unique_blob_count, 2)
                self.assertEqual(parsed.unique_blob_bytes, 9)

    def test_tree_framing_and_scalar_grammar_rejects_malformed_records(self):
        oid = "a" * 40
        valid = self.tree_record("file.py", oid, 1)
        attacks = (
            valid[:-1],
            valid + b"\0",
            b"100644\tblob\t" + oid.encode("ascii") + b"\t1\0",
            b"100600\tblob\t" + oid.encode("ascii") + b"\t1\tfile.py\0",
            b"100644\ttree\t" + oid.encode("ascii") + b"\t1\tfile.py\0",
            b"100644\tblob\t" + ("A" * 40).encode("ascii") + b"\t1\tfile.py\0",
            b"100644\tblob\t" + oid.encode("ascii") + b"\t01\tfile.py\0",
            b"100644\tblob\t" + oid.encode("ascii") + b"\t-1\tfile.py\0",
            b"100644\tbl\xc3\xb6b\t" + oid.encode("ascii") + b"\t1\tfile.py\0",
        )
        for payload in attacks:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                TaskSnapshotError, "^task_tree_invalid$"
            ):
                _parse_task_tree(payload, "sha1", TaskSnapshotPolicy())

    def test_huge_canonical_size_fails_closed_before_integer_conversion(self):
        payload = (
            b"100644\tblob\t"
            + b"a" * 40
            + b"\t"
            + b"9" * 5000
            + b"\tfile.py\0"
        )
        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_blob_limit$"
        ) as raised:
            _parse_task_tree(payload, "sha1", TaskSnapshotPolicy())
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)

    def test_raw_path_caps_precede_decode_and_unicode_work(self):
        oid = b"a" * 40
        prefix = b"100644\tblob\t" + oid + b"\t0\t"
        policy = TaskSnapshotPolicy()
        over_cap_paths = (
            b"a" * (policy.max_relative_path_bytes + 1),
            b"a" * (policy.max_component_bytes + 1),
            b"/".join(b"a" for _ in range(policy.max_tree_depth + 1)),
        )
        for raw_path in over_cap_paths:
            with self.subTest(length=len(raw_path)), patch(
                "scripts.live_eval.task_snapshot.unicodedata.normalize",
                side_effect=AssertionError("normalization must not run"),
            ), self.assertRaisesRegex(
                TaskSnapshotError, "^task_tree_limit$"
            ):
                _parse_task_tree(
                    prefix + raw_path + b"\0",
                    "sha1",
                    policy,
                )

        invalid_utf8 = b"\xff" * (policy.max_relative_path_bytes + 1)
        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_tree_limit$"
        ):
            _parse_task_tree(prefix + invalid_utf8 + b"\0", "sha1", policy)

    def test_record_flood_is_bounded_and_fails_with_tree_limit(self):
        payload = b"".join(
            self.tree_record(
                "f{:04d}".format(index),
                "{:040x}".format(index + 1),
                0,
            )
            for index in range(1000)
        )
        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_tree_limit$"
        ):
            _parse_task_tree(
                payload,
                "sha1",
                TaskSnapshotPolicy(max_files=2),
            )

    def test_path_grammar_aliases_and_file_directory_conflicts_fail(self):
        oid = "a" * 40
        invalid_paths = (
            "/absolute",
            "trailing/",
            "empty//part",
            ".",
            "..",
            "a/./b",
            "a/../b",
            "back\\slash",
            "trailing.",
            "trailing ",
            "tab\tname",
            "zero\u200bwidth",
            "e\u0301.py",
        )
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaisesRegex(
                TaskSnapshotError, "^task_tree_invalid$"
            ):
                _parse_task_tree(
                    self.tree_record(path, oid, 1),
                    "sha1",
                    TaskSnapshotPolicy(),
                )

        invalid_utf8 = (
            b"100644\tblob\t"
            + oid.encode("ascii")
            + b"\t1\tbad-\xff\0"
        )
        collisions = (
            self.tree_record("Straße.py", oid, 1)
            + self.tree_record("STRASSE.py", "b" * 40, 1),
            self.tree_record("same", oid, 1)
            + self.tree_record("same", oid, 1),
            self.tree_record("parent", oid, 1)
            + self.tree_record("parent/child", "b" * 40, 1),
            self.tree_record("parent/child", oid, 1)
            + self.tree_record("parent", "b" * 40, 1),
        )
        for payload in (invalid_utf8,) + collisions:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                TaskSnapshotError, "^task_tree_invalid$"
            ):
                _parse_task_tree(payload, "sha1", TaskSnapshotPolicy())

    def test_exclusion_scopes_reject_only_the_enumerated_boundaries(self):
        oid = "a" * 40
        denied = (
            ".GiT/config",
            "src/AGENTS.MD",
            "src/agents.override.md",
            "src/.CoDeX/config.toml",
            "src/.agents/role.md",
            "src/.claude/config",
            "src/.mcp/config",
            ".git-hooks/pre-commit",
            "Hooks/pre-commit",
            "PLUGINS/tool.py",
            "src/.mcp.json",
            "src/mcp.json",
            "src/.env",
            "src/.ENV.LOCAL",
            "src/.npmrc",
            "src/.pypirc",
            "src/CREDENTIALS.JSON",
            "src/secrets.json",
            "src/.GIT",
        )
        for path in denied:
            with self.subTest(path=path), self.assertRaisesRegex(
                TaskSnapshotError, "^task_tree_invalid$"
            ):
                _parse_task_tree(
                    self.tree_record(path, oid, 1),
                    "sha1",
                    TaskSnapshotPolicy(),
                )

        allowed = b"".join(
            (
                self.tree_record(".agents", oid, 1),
                self.tree_record(".claude", oid, 1),
                self.tree_record(".codex", oid, 1),
                self.tree_record(".env.production", oid, 1),
                self.tree_record(".git-hooks", oid, 1),
                self.tree_record(".mcp", oid, 1),
                self.tree_record("hooks", oid, 1),
                self.tree_record("plugins", oid, 1),
                self.tree_record("src/.git-hooks/tool.py", oid, 1),
                self.tree_record("src/hooks/tool.py", oid, 1),
                self.tree_record("src/plugins/tool.py", oid, 1),
            )
        )
        parsed = _parse_task_tree(allowed, "sha1", TaskSnapshotPolicy())
        self.assertEqual(parsed.file_count, 11)

    def test_public_tree_entry_rejects_unvalidated_path(self):
        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_tree_invalid$"
        ):
            TaskTreeEntry(
                path="../escape",
                git_mode="100644",
                blob_oid="a" * 40,
                size=1,
                content_digest="sha256:" + "b" * 64,
            )

    def test_complete_tree_limits_are_decided_before_blob_loading(self):
        accepted = b"".join(
            self.tree_record(
                "f{:03d}".format(index),
                "{:040x}".format(index + 1),
                0,
            )
            for index in range(256)
        )
        parsed = _parse_task_tree(
            accepted,
            "sha1",
            TaskSnapshotPolicy(max_files=256),
        )
        self.assertEqual(parsed.unique_blob_count, 256)

        attacks = (
            (
                accepted
                + self.tree_record("overflow", "{:040x}".format(257), 0),
                TaskSnapshotPolicy(max_files=257),
                "task_blob_limit",
            ),
            (
                self.tree_record("one", "a" * 40, 0)
                + self.tree_record("two", "a" * 40, 0),
                TaskSnapshotPolicy(max_files=1),
                "task_tree_limit",
            ),
            (
                self.tree_record("dir/file", "a" * 40, 0),
                TaskSnapshotPolicy(max_tree_entries=2),
                "task_tree_limit",
            ),
            (
                self.tree_record("large", "a" * 40, 2),
                TaskSnapshotPolicy(max_file_bytes=1),
                "task_blob_limit",
            ),
        )
        for payload, policy, expected in attacks:
            with self.subTest(expected=expected), patch(
                "scripts.live_eval.task_snapshot._run_git"
            ) as run_git, self.assertRaisesRegex(
                TaskSnapshotError, "^" + expected + "$"
            ):
                parsed = _parse_task_tree(payload, "sha1", policy)
                _load_task_blobs(
                    Path("/repo/.git"),
                    Path("/repo/.git/config"),
                    parsed,
                    policy,
                )
            run_git.assert_not_called()

    def test_path_and_logical_byte_limits_use_fixed_classifications(self):
        cases = (
            (
                self.tree_record("ab", "a" * 40, 0),
                TaskSnapshotPolicy(max_component_bytes=1),
                "task_tree_limit",
            ),
            (
                self.tree_record("ab/c", "a" * 40, 0),
                TaskSnapshotPolicy(
                    max_component_bytes=3,
                    max_relative_path_bytes=3,
                ),
                "task_tree_limit",
            ),
            (
                self.tree_record("a/b", "a" * 40, 0),
                TaskSnapshotPolicy(max_tree_depth=1),
                "task_tree_limit",
            ),
            (
                self.tree_record("one", "a" * 40, 1)
                + self.tree_record("two", "a" * 40, 1),
                TaskSnapshotPolicy(
                    max_file_bytes=1,
                    max_total_bytes=1,
                ),
                "task_blob_limit",
            ),
        )
        for payload, policy, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(
                TaskSnapshotError, "^" + expected + "$"
            ):
                _parse_task_tree(payload, "sha1", policy)

    def test_all_semantic_caps_are_inclusive(self):
        oid = "a" * 40
        accepted = (
            (
                self.tree_record("ab", oid, 0),
                TaskSnapshotPolicy(max_component_bytes=2),
            ),
            (
                self.tree_record("ab/c", oid, 0),
                TaskSnapshotPolicy(
                    max_component_bytes=3,
                    max_relative_path_bytes=4,
                ),
            ),
            (
                self.tree_record("a/b", oid, 0),
                TaskSnapshotPolicy(max_tree_depth=2),
            ),
            (
                self.tree_record("dir/file", oid, 0),
                TaskSnapshotPolicy(max_tree_entries=3),
            ),
            (
                self.tree_record("one", oid, 1),
                TaskSnapshotPolicy(
                    max_file_bytes=1,
                    max_total_bytes=1,
                ),
            ),
            (
                self.tree_record("one", oid, 1)
                + self.tree_record("two", oid, 1),
                TaskSnapshotPolicy(
                    max_file_bytes=1,
                    max_total_bytes=2,
                ),
            ),
        )
        for payload, policy in accepted:
            with self.subTest(policy=policy):
                self.assertGreaterEqual(
                    _parse_task_tree(payload, "sha1", policy).file_count,
                    1,
                )

    def test_duplicate_classification_precedes_file_cap(self):
        record = self.tree_record("same", "a" * 40, 0)
        with self.assertRaisesRegex(
            TaskSnapshotError, "^task_tree_invalid$"
        ):
            _parse_task_tree(
                record + record,
                "sha1",
                TaskSnapshotPolicy(max_files=1),
            )

    def test_repeated_oid_size_mismatch_fails_before_loading(self):
        payload = (
            self.tree_record("one", "a" * 40, 1)
            + self.tree_record("two", "a" * 40, 2)
        )
        with patch(
            "scripts.live_eval.task_snapshot._run_git"
        ) as run_git, self.assertRaisesRegex(
            TaskSnapshotError, "^task_tree_invalid$"
        ):
            parsed = _parse_task_tree(
                payload, "sha1", TaskSnapshotPolicy()
            )
            _load_task_blobs(
                Path("/repo/.git"),
                Path("/repo/.git/config"),
                parsed,
                TaskSnapshotPolicy(),
            )
        run_git.assert_not_called()

    def test_blob_loader_rejects_short_long_and_oid_mismatch(self):
        content = b"good"
        oid = self.blob_oid(content, "sha1")
        parsed = _parse_task_tree(
            self.tree_record("file", oid, len(content)),
            "sha1",
            TaskSnapshotPolicy(),
        )
        for returned in (b"bad", b"longer", b"evil"):
            with self.subTest(returned=returned), patch(
                "scripts.live_eval.task_snapshot._run_git",
                return_value=returned,
            ), self.assertRaisesRegex(
                TaskSnapshotError, "^task_blob_invalid$"
            ):
                _load_task_blobs(
                    Path("/repo/.git"),
                    Path("/repo/.git/config"),
                    parsed,
                    TaskSnapshotPolicy(),
                )

    def test_entry_document_separates_logical_and_unique_aggregates(self):
        content = b"x"
        oid = self.blob_oid(content, "sha1")
        parsed = _parse_task_tree(
            self.tree_record("b", oid, 1)
            + self.tree_record("a", oid, 1, "100755"),
            "sha1",
            TaskSnapshotPolicy(),
        )
        with patch(
            "scripts.live_eval.task_snapshot._run_git",
            return_value=content,
        ):
            entries, blobs = _load_task_blobs(
                Path("/repo/.git"),
                Path("/repo/.git/config"),
                parsed,
                TaskSnapshotPolicy(),
            )
        commit_oid = "b" * 40
        tree_oid = "c" * 40
        document = _task_entry_document(
            commit_oid, tree_oid, parsed, entries
        )
        content_digest = "sha256:" + hashlib.sha256(content).hexdigest()
        self.assertEqual(
            document,
            {
                "commit_oid": commit_oid,
                "directory_count": 1,
                "document_type": "task-tree-entries-v1",
                "entries": [
                    {
                        "blob_oid": oid,
                        "content_digest": content_digest,
                        "git_mode": "100755",
                        "path": "a",
                        "size": 1,
                    },
                    {
                        "blob_oid": oid,
                        "content_digest": content_digest,
                        "git_mode": "100644",
                        "path": "b",
                        "size": 1,
                    },
                ],
                "file_count": 2,
                "logical_total_bytes": 2,
                "object_format": "sha1",
                "schema_version": 1,
                "tree_entry_count": 3,
                "tree_oid": tree_oid,
                "unique_blob_bytes": 1,
                "unique_blob_count": 1,
            },
        )
        self.assertEqual(
            _task_entry_digest(commit_oid, tree_oid, parsed, entries),
            "sha256:" + hashlib.sha256(
                task_snapshot_module.canonical_bytes(document)
            ).hexdigest(),
        )
        self.assertEqual(type(entries[0]), TaskTreeEntry)
        self.assertEqual(type(blobs), MappingProxyType)


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

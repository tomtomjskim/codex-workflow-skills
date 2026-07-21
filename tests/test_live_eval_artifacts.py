import os
from pathlib import Path
import shutil
import stat
import tempfile
import traceback
import unittest
from unittest import mock

import scripts.live_eval.artifacts as artifacts_module
from scripts.live_eval.artifacts import RedactingWriter, RedactionError


class FailingRedactor:
    def redact(self, text):
        raise RuntimeError("redaction failed")


class InvalidJSONRedactor:
    def redact(self, text):
        return '{"value":NaN}'


class LeakingFailureRedactor:
    def redact(self, text):
        raise RuntimeError("SECRET_NAME secret-value /home/private-user")


class PassThroughRedactor:
    def redact(self, text):
        return text


class ReintroducingRedactor:
    def redact(self, text):
        return '{"value":"secret-value /home/private-user"}'


class EscapedSecretRedactor:
    def redact(self, text):
        return '{"value":"secret\\u002dvalue"}'


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def test_finalize_redacts_split_secret_and_home_before_exclusive_write(self):
        writer = RedactingWriter(
            self.directory,
            {"OPENAI_API_KEY": "secret-value"},
            artifact_name="result.jsonl",
        )
        encoded = '{"message":"café secret-value /home/example OPENAI_API_KEY"}\n'.encode()
        secret_split = encoded.index(b"secret-value") + 6
        unicode_split = encoded.index("é".encode()) + 1

        writer.write(encoded[:unicode_split])
        writer.write(encoded[unicode_split:secret_split])
        writer.write(encoded[secret_split:])
        self.assertFalse(writer.path.exists())

        path = writer.finalize()

        content = path.read_text(encoding="utf-8")
        self.assertIn("café", content)
        self.assertNotIn("secret-value", content)
        self.assertNotIn("OPENAI_API_KEY", content)
        self.assertNotIn("/home/example", content)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_structurally_redacts_decoded_keys_values_lists_and_escapes(self):
        writer = RedactingWriter(
            self.directory,
            {
                "SECRET_NAME": "secret-value",
                "SLASH": "a/b",
                "UNICODE": "café",
                "QUOTE": 'say"hi',
                "BACKSLASH": "a\\b",
                "SHORT": "secret",
                "LONG": "secret-value",
            },
            artifact_name="structured.jsonl",
        )
        raw = (
            b'{"SECRET_NAME":{"items":["secret-value","a\\/b",'
            b'"caf\\u00e9","say\\\"hi","a\\\\b",'
            b'"/home/private-user"],"plain":"keep"}}\n'
        )
        for index in range(0, len(raw), 3):
            writer.write(raw[index : index + 3])

        content = writer.finalize().read_text(encoding="utf-8")

        self.assertEqual(content.count("\n"), 1)
        self.assertNotIn(": ", content)
        for literal in (
            "SECRET_NAME",
            "secret-value",
            "a/b",
            "café",
            'say"hi',
            "a\\b",
            "/home/private-user",
            "[REDACTED]-value",
        ):
            self.assertNotIn(literal, content)
        self.assertIn('"plain":"keep"', content)

    def test_rejects_invalid_jsonl_and_custom_redactor_output(self):
        invalid = RedactingWriter(self.directory, {}, artifact_name="invalid")
        invalid.write(b'{"value":}\n')
        with self.assertRaises(RedactionError):
            invalid.finalize()

        custom = RedactingWriter(
            self.directory,
            {},
            artifact_name="custom",
            redactor=InvalidJSONRedactor(),
        )
        custom.write(b'{"value":"ok"}\n')
        with self.assertRaises(RedactionError):
            custom.finalize()

        self.assertFalse(invalid.path.exists())
        self.assertFalse(custom.path.exists())

    def test_builtin_redaction_is_final_safety_after_pass_through_custom(self):
        writer = RedactingWriter(
            self.directory,
            {"SECRET_NAME": "secret-value"},
            artifact_name="pass-through",
            redactor=PassThroughRedactor(),
        )
        writer.write(b'{"SECRET_NAME":"secret-value /home/private-user"}\n')

        content = writer.finalize().read_text(encoding="utf-8")

        self.assertNotIn("SECRET_NAME", content)
        self.assertNotIn("secret-value", content)
        self.assertNotIn("/home/private-user", content)

    def test_builtin_redaction_removes_secret_reintroduced_by_custom(self):
        writer = RedactingWriter(
            self.directory,
            {"SECRET_NAME": "secret-value"},
            artifact_name="reintroduced",
            redactor=ReintroducingRedactor(),
        )
        writer.write(b'{"value":"safe"}\n')

        content = writer.finalize().read_text(encoding="utf-8")

        self.assertNotIn("secret-value", content)
        self.assertNotIn("/home/private-user", content)

    def test_builtin_redaction_removes_escaped_custom_secret(self):
        writer = RedactingWriter(
            self.directory,
            {"SECRET_NAME": "secret-value"},
            artifact_name="escaped-custom",
            redactor=EscapedSecretRedactor(),
        )
        writer.write(b'{"value":"safe"}\n')

        content = writer.finalize().read_text(encoding="utf-8")

        self.assertNotIn("secret-value", content)
        self.assertNotIn("secret\\u002dvalue", content)

    def test_replacement_never_overlaps_configured_secret(self):
        writer = RedactingWriter(
            self.directory,
            {"REDACTED": "[REDACTED]"},
            artifact_name="marker-overlap",
            redactor=PassThroughRedactor(),
        )
        writer.write(b'{"first":"REDACTED","second":"[REDACTED]"}\n')

        content = writer.finalize().read_text(encoding="utf-8")

        self.assertNotIn("REDACTED", content)
        self.assertNotIn("[REDACTED]", content)

    def test_redacted_mapping_key_collision_fails_closed(self):
        writer = RedactingWriter(
            self.directory,
            {"SECRET_NAME": "secret"},
            artifact_name="key-collision",
            redactor=PassThroughRedactor(),
        )
        writer.write(b'{"secret":"first","":"second"}\n')

        with self.assertRaises(RedactionError):
            writer.finalize()

        self.assertFalse(writer.path.exists())

    def test_redaction_errors_hide_raw_values_from_traceback(self):
        cases = (
            ("decode", None, b'{"SECRET_NAME":"secret-value /home/private-user"}\xff'),
            ("parse", None, b'{"SECRET_NAME":"secret-value /home/private-user",}\n'),
            (
                "custom",
                LeakingFailureRedactor(),
                b'{"SECRET_NAME":"secret-value /home/private-user"}\n',
            ),
        )
        for name, redactor, raw in cases:
            with self.subTest(name=name):
                writer = RedactingWriter(
                    self.directory,
                    {"SECRET_NAME": "secret-value"},
                    artifact_name=name,
                    redactor=redactor,
                )
                try:
                    writer.write(raw)
                    writer.finalize()
                except RedactionError:
                    rendered = traceback.format_exc()
                else:
                    self.fail("expected redaction failure")
                self.assertNotIn("SECRET_NAME", rendered)
                self.assertNotIn("secret-value", rendered)
                self.assertNotIn("/home/private-user", rendered)

    def test_raw_byte_limit_fails_closed_and_clears_buffer(self):
        writer = RedactingWriter(self.directory, {}, max_raw_bytes=3)
        writer.write(b"abc")

        with self.assertRaises(RedactionError):
            writer.write(b"d")

        self.assertEqual(writer.buffered_bytes, 0)
        self.assertIsNone(writer.retained_path)
        self.assertFalse(writer.path.exists())

    def test_redaction_failure_retains_nothing(self):
        writer = RedactingWriter(self.directory, {}, redactor=FailingRedactor())
        writer.write(b'{"value":"raw"}\n')

        with self.assertRaises(RedactionError):
            writer.finalize()

        self.assertEqual(writer.buffered_bytes, 0)
        self.assertIsNone(writer.retained_path)
        self.assertFalse(writer.path.exists())

    def test_invalid_utf8_fails_closed(self):
        writer = RedactingWriter(self.directory, {})

        with self.assertRaises(RedactionError):
            writer.write(b"\xff")

        self.assertEqual(writer.buffered_bytes, 0)
        self.assertFalse(writer.path.exists())

    def test_existing_final_file_is_never_overwritten_or_removed(self):
        existing = self.directory / "result.jsonl"
        existing.write_bytes(b"owned")
        writer = RedactingWriter(self.directory, {}, artifact_name=existing.name)
        writer.write(b'{"value":"new"}\n')

        with self.assertRaises(RedactionError):
            writer.finalize()

        self.assertEqual(existing.read_bytes(), b"owned")

    def test_write_and_fsync_failures_remove_only_created_artifact(self):
        for failure in ("write", "fsync"):
            with self.subTest(failure=failure):
                name = failure + ".jsonl"
                writer = RedactingWriter(self.directory, {}, artifact_name=name)
                writer.write(b'{"value":"retained"}\n')
                target = "scripts.live_eval.artifacts.os." + failure
                with mock.patch(target, side_effect=OSError(failure)):
                    with self.assertRaises(RedactionError):
                        writer.finalize()
                self.assertFalse((self.directory / name).exists())
                self.assertIsNone(writer.retained_path)

    def test_fstat_and_link_failures_leave_no_owned_artifact(self):
        for failure in ("fstat", "link"):
            with self.subTest(failure=failure):
                name = failure + ".jsonl"
                writer = RedactingWriter(self.directory, {}, artifact_name=name)
                writer.write(b'{"value":"retained"}\n')
                target = "scripts.live_eval.artifacts.os." + failure
                with mock.patch(target, side_effect=OSError(failure)):
                    with self.assertRaises(RedactionError):
                        writer.finalize()
                self.assertFalse((self.directory / name).exists())
                self.assertEqual(writer.buffered_bytes, 0)

    def test_staging_fstat_failure_immediately_unlinks_created_leaf(self):
        writer = RedactingWriter(self.directory, {}, artifact_name="fstat-only.jsonl")
        writer.write(b'{"value":"retained"}\n')
        original_fstat = artifacts_module.os.fstat
        failed = []

        def fail_staging_fstat(fd):
            info = original_fstat(fd)
            if stat.S_ISREG(info.st_mode) and not failed:
                failed.append(True)
                raise OSError("staging fstat")
            return info

        with mock.patch("scripts.live_eval.artifacts.os.fstat", side_effect=fail_staging_fstat):
            with self.assertRaises(RedactionError):
                writer.finalize()

        self.assertEqual(list(self.directory.glob(".live-eval-stage-*")), [])
        self.assertFalse(writer.path.exists())

    def test_staging_fstat_cleanup_failure_reports_warning_without_overclaim(self):
        writer = RedactingWriter(self.directory, {}, artifact_name="fstat-warning.jsonl")
        writer.write(b'{"value":"retained"}\n')
        original_fstat = artifacts_module.os.fstat
        failed = []

        def fail_staging_fstat(fd):
            info = original_fstat(fd)
            if stat.S_ISREG(info.st_mode) and not failed:
                failed.append(True)
                raise OSError("staging fstat")
            return info

        with mock.patch("scripts.live_eval.artifacts.os.fstat", side_effect=fail_staging_fstat), mock.patch(
            "scripts.live_eval.artifacts.os.unlink", side_effect=OSError("cleanup unlink")
        ):
            with self.assertRaises(RedactionError) as raised:
                writer.finalize()

        self.assertTrue(hasattr(raised.exception, "cleanup_warnings"))
        self.assertTrue(raised.exception.cleanup_warnings)
        self.assertEqual(len(list(self.directory.glob(".live-eval-stage-*"))), 1)
        self.assertFalse(writer.path.exists())

    def test_competing_final_file_created_during_link_is_preserved(self):
        writer = RedactingWriter(self.directory, {}, artifact_name="race.jsonl")
        writer.write(b'{"value":"owned"}\n')

        def competing_link(source, target, **kwargs):
            fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=kwargs["dst_dir_fd"],
            )
            os.write(fd, b"competitor")
            os.close(fd)
            raise FileExistsError("competitor won")

        with mock.patch("scripts.live_eval.artifacts.os.link", side_effect=competing_link):
            with self.assertRaises(RedactionError):
                writer.finalize()

        self.assertEqual((self.directory / "race.jsonl").read_bytes(), b"competitor")

    def test_recreated_staging_file_is_preserved_on_cleanup(self):
        writer = RedactingWriter(self.directory, {}, artifact_name="result.jsonl")
        writer.write(b'{"value":"owned"}\n')
        original_write = artifacts_module.os.write
        replacements = []

        def swap_staging_then_fail(fd, data):
            names = [name for name in os.listdir(self.directory) if name.startswith(".live-eval-stage-")]
            if names:
                name = names[0]
                moved = name + ".owned"
                os.rename(name, moved, src_dir_fd=writer._directory_fd, dst_dir_fd=writer._directory_fd)
                replacement = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=writer._directory_fd,
                )
                original_write(replacement, b"competitor")
                os.close(replacement)
                replacements.append(name)
                raise OSError("simulated write failure")
            return original_write(fd, data)

        with mock.patch("scripts.live_eval.artifacts.os.write", side_effect=swap_staging_then_fail):
            with self.assertRaises(RedactionError):
                writer.finalize()

        self.assertEqual(len(replacements), 1)
        self.assertEqual((self.directory / replacements[0]).read_bytes(), b"competitor")

    def test_parent_directory_swap_is_detected_before_publish(self):
        original = self.directory
        moved = original.parent / (original.name + "-moved")
        writer = RedactingWriter(original, {}, artifact_name="result.jsonl")
        writer.write(b'{"value":"owned"}\n')
        original.rename(moved)
        original.mkdir(mode=0o700)
        try:
            with self.assertRaises(RedactionError):
                writer.finalize()

            self.assertFalse((original / "result.jsonl").exists())
            self.assertFalse((moved / "result.jsonl").exists())
        finally:
            shutil.rmtree(str(original))
            moved.rename(original)

    def test_parent_directory_swap_during_finalize_is_detected(self):
        original = self.directory
        moved = original.parent / (original.name + "-moved-during-finalize")
        writer = RedactingWriter(original, {}, artifact_name="result.jsonl")
        writer.write(b'{"value":"owned"}\n')
        original_write = artifacts_module.os.write
        swapped = []

        def write_then_swap(fd, data):
            written = original_write(fd, data)
            if not swapped:
                original.rename(moved)
                original.mkdir(mode=0o700)
                swapped.append(True)
            return written

        try:
            with mock.patch("scripts.live_eval.artifacts.os.write", side_effect=write_then_swap):
                with self.assertRaises(RedactionError):
                    writer.finalize()
            self.assertFalse((original / "result.jsonl").exists())
            self.assertFalse((moved / "result.jsonl").exists())
        finally:
            shutil.rmtree(str(original))
            moved.rename(original)

    def test_fchmod_failure_cleans_only_owned_staging_file(self):
        writer = RedactingWriter(self.directory, {}, artifact_name="result.jsonl")
        writer.write(b'{"value":"owned"}\n')

        with mock.patch("scripts.live_eval.artifacts.os.fchmod", side_effect=OSError("fchmod")):
            with self.assertRaises(RedactionError):
                writer.finalize()

        self.assertFalse(writer.path.exists())
        self.assertEqual(list(self.directory.glob(".live-eval-stage-*")), [])

    def test_owned_staging_unlink_failure_fails_closed(self):
        writer = RedactingWriter(self.directory, {}, artifact_name="result.jsonl")
        writer.write(b'{"value":"owned"}\n')
        original_unlink = artifacts_module.os.unlink
        failed = []

        def fail_first_owned_unlink(path, **kwargs):
            if not failed and path.startswith(".live-eval-stage-"):
                failed.append(True)
                raise OSError("simulated unlink failure")
            return original_unlink(path, **kwargs)

        with mock.patch("scripts.live_eval.artifacts.os.unlink", side_effect=fail_first_owned_unlink):
            with self.assertRaises(RedactionError):
                writer.finalize()

        self.assertFalse(writer.path.exists())
        self.assertEqual(list(self.directory.glob(".live-eval-stage-*")), [])

    def test_untrusted_directory_is_rejected(self):
        self.directory.chmod(0o755)

        with self.assertRaises(RedactionError):
            RedactingWriter(self.directory, {})

    def test_symlink_directory_is_rejected(self):
        link = self.directory.parent / (self.directory.name + "-link")
        os.symlink(str(self.directory), str(link))
        try:
            with self.assertRaises(RedactionError):
                RedactingWriter(link, {})
        finally:
            link.unlink()

    def test_writes_after_finalize_or_failure_are_rejected(self):
        complete = RedactingWriter(self.directory, {}, artifact_name="complete")
        complete.write(b'{"value":"ok"}\n')
        complete.finalize()
        failed = RedactingWriter(self.directory, {}, artifact_name="failed", max_raw_bytes=1)
        with self.assertRaises(RedactionError):
            failed.write(b"no")

        for writer in (complete, failed):
            with self.assertRaises(RedactionError):
                writer.write(b"later")

    def test_close_abort_and_context_manager_zero_and_close_resources(self):
        closed = RedactingWriter(self.directory, {}, artifact_name="closed")
        self.assertTrue(hasattr(closed, "_directory_fd"))
        directory_fd = closed._directory_fd
        closed.write(b'{"secret":"raw"}\n')
        closed.close()
        closed.close()
        self.assertEqual(closed.buffered_bytes, 0)
        with self.assertRaises(OSError):
            os.fstat(directory_fd)
        with self.assertRaises(RedactionError):
            closed.write(b"later")
        with self.assertRaises(RedactionError):
            closed.finalize()

        aborted = RedactingWriter(self.directory, {}, artifact_name="aborted")
        aborted.write(b'{"secret":"raw"}\n')
        aborted.abort()
        self.assertEqual(aborted.buffered_bytes, 0)
        self.assertFalse(aborted.path.exists())

        with RedactingWriter(self.directory, {}, artifact_name="context") as contextual:
            contextual.write(b'{"secret":"raw"}\n')
        self.assertEqual(contextual.buffered_bytes, 0)
        with self.assertRaises(RedactionError):
            contextual.finalize()


if __name__ == "__main__":
    unittest.main()

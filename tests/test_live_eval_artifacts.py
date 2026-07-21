import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from scripts.live_eval.artifacts import RedactingWriter, RedactionError


class FailingRedactor:
    def redact(self, text):
        raise RuntimeError("redaction failed")


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
        writer.write(b"raw")

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
        writer.write(b"new")

        with self.assertRaises(RedactionError):
            writer.finalize()

        self.assertEqual(existing.read_bytes(), b"owned")

    def test_write_and_fsync_failures_remove_only_created_artifact(self):
        for failure in ("write", "fsync"):
            with self.subTest(failure=failure):
                name = failure + ".jsonl"
                writer = RedactingWriter(self.directory, {}, artifact_name=name)
                writer.write(b"retained")
                target = "scripts.live_eval.artifacts.os." + failure
                with mock.patch(target, side_effect=OSError(failure)):
                    with self.assertRaises(RedactionError):
                        writer.finalize()
                self.assertFalse((self.directory / name).exists())
                self.assertIsNone(writer.retained_path)

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
        complete.write(b"ok")
        complete.finalize()
        failed = RedactingWriter(self.directory, {}, artifact_name="failed", max_raw_bytes=1)
        with self.assertRaises(RedactionError):
            failed.write(b"no")

        for writer in (complete, failed):
            with self.assertRaises(RedactionError):
                writer.write(b"later")


if __name__ == "__main__":
    unittest.main()

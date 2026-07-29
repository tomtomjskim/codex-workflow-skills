import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.live_eval.private_jsonl_ledger import (
    PrivateJSONLLedger,
    PrivateJSONLLedgerError,
)
from scripts.workflow_coordination.canonical_json import canonical_bytes


GENESIS = "sha256:" + ("0" * 64)


def record(previous_record_hash, sequence):
    return canonical_bytes(
        {
            "document_type": "phase_b0_ledger_sentinel",
            "previous_record_hash": previous_record_hash,
            "schema_version": 1,
            "sequence": sequence,
        }
    )


class PrivateJSONLLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "ledger"
        self.directory.mkdir(mode=0o700)
        os.chmod(self.directory, 0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def read_ledger(self):
        return (self.directory / "runtime.jsonl").read_bytes()

    def test_fresh_ledger_uses_genesis_and_appends_canonical_line(self):
        with PrivateJSONLLedger.open(self.directory, GENESIS) as ledger:
            self.assertEqual(ledger.state.record_count, 0)
            self.assertEqual(ledger.state.total_bytes, 0)
            self.assertEqual(ledger.state.last_record_hash, GENESIS)
            line = record(GENESIS, 1)
            state = ledger.append(line)

        expected = line + b"\n"
        self.assertEqual(self.read_ledger(), expected)
        self.assertEqual(state.record_count, 1)
        self.assertEqual(state.total_bytes, len(expected))
        self.assertEqual(
            state.last_record_hash, "sha256:" + hashlib.sha256(line).hexdigest()
        )

    def test_reopen_validates_history_and_continues_from_prior_head(self):
        first = record(GENESIS, 1)
        with PrivateJSONLLedger.open(self.directory, GENESIS) as ledger:
            first_state = ledger.append(first)
        second = record(first_state.last_record_hash, 2)
        with PrivateJSONLLedger.open(self.directory, GENESIS) as ledger:
            self.assertEqual(ledger.state, first_state)
            state = ledger.append(second)
        self.assertEqual(self.read_ledger(), first + b"\n" + second + b"\n")
        self.assertEqual(state.record_count, 2)

    def test_context_manager_and_close_are_idempotent(self):
        ledger = PrivateJSONLLedger.open(self.directory, GENESIS)
        with ledger:
            ledger.append(record(GENESIS, 1))
        ledger.close()
        ledger.close()
        with self.assertRaisesRegex(PrivateJSONLLedgerError, "closed"):
            ledger.append(record(GENESIS, 2))

    def test_invalid_append_inputs_do_not_change_ledger(self):
        with PrivateJSONLLedger.open(self.directory, GENESIS) as ledger:
            valid = record(GENESIS, 1)
            invalid_inputs = (
                record("sha256:" + "1" * 64, 1),
                b'{"sequence":1, "previous_record_hash":"' + GENESIS.encode() + b'"}',
                b'{"previous_record_hash":"' + GENESIS.encode() + b'","x":1,"x":2}',
                b'{"previous_record_hash":"' + GENESIS.encode() + b'","x":1.5}',
                b"[]",
                b'{"previous_record_hash":true}',
            )
            for value in invalid_inputs:
                with self.subTest(value=value):
                    before = self.read_ledger()
                    with self.assertRaises(PrivateJSONLLedgerError):
                        ledger.append(value)
                    self.assertEqual(self.read_ledger(), before)
            ledger.append(valid)

    def test_record_and_total_byte_limits_do_not_write(self):
        with PrivateJSONLLedger.open(
            self.directory, GENESIS, max_records=1, max_bytes=1000, max_record_bytes=10
        ) as ledger:
            with self.assertRaises(PrivateJSONLLedgerError):
                ledger.append(record(GENESIS, 1))
            self.assertEqual(self.read_ledger(), b"")
        line = record(GENESIS, 1)
        with PrivateJSONLLedger.open(
            self.directory, GENESIS, max_records=2, max_bytes=len(line), max_record_bytes=len(line)
        ) as ledger:
            with self.assertRaises(PrivateJSONLLedgerError):
                ledger.append(line)
            self.assertEqual(self.read_ledger(), b"")
        with PrivateJSONLLedger.open(
            self.directory, GENESIS, max_records=1, max_bytes=1000, max_record_bytes=1000
        ) as ledger:
            ledger.append(line)
            with self.assertRaises(PrivateJSONLLedgerError):
                ledger.append(record(ledger.state.last_record_hash, 2))

    def test_boolean_limits_are_rejected(self):
        for keyword in ("max_records", "max_bytes", "max_record_bytes"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(PrivateJSONLLedgerError):
                    PrivateJSONLLedger.open(self.directory, GENESIS, **{keyword: True})
        self.assertFalse((self.directory / "runtime.jsonl").exists())
        self.assertFalse((self.directory / "runtime.lock").exists())

    def test_rejects_partial_tail_and_wrong_historical_hash(self):
        (self.directory / "runtime.jsonl").write_bytes(record(GENESIS, 1))
        (self.directory / "runtime.lock").write_bytes(b"")
        os.chmod(self.directory / "runtime.jsonl", 0o600)
        os.chmod(self.directory / "runtime.lock", 0o600)
        with self.assertRaises(PrivateJSONLLedgerError):
            PrivateJSONLLedger.open(self.directory, GENESIS)
        (self.directory / "runtime.jsonl").write_bytes(
            record("sha256:" + "f" * 64, 1) + b"\n"
        )
        with self.assertRaises(PrivateJSONLLedgerError):
            PrivateJSONLLedger.open(self.directory, GENESIS)

    def test_rejects_untrusted_directory_or_file_mode(self):
        os.chmod(self.directory, 0o755)
        with self.assertRaises(PrivateJSONLLedgerError):
            PrivateJSONLLedger.open(self.directory, GENESIS)
        os.chmod(self.directory, 0o700)
        with PrivateJSONLLedger.open(self.directory, GENESIS):
            pass
        os.chmod(self.directory / "runtime.jsonl", 0o644)
        with self.assertRaises(PrivateJSONLLedgerError):
            PrivateJSONLLedger.open(self.directory, GENESIS)

    def test_rejects_symlink_and_hardlink_fixed_files(self):
        target = self.directory / "target"
        target.write_bytes(b"")
        os.chmod(target, 0o600)
        (self.directory / "runtime.jsonl").symlink_to(target.name)
        with self.assertRaises(PrivateJSONLLedgerError):
            PrivateJSONLLedger.open(self.directory, GENESIS)
        (self.directory / "runtime.jsonl").unlink()
        (self.directory / "runtime.lock").write_bytes(b"")
        (self.directory / "runtime.jsonl").write_bytes(b"")
        os.chmod(self.directory / "runtime.jsonl", 0o600)
        os.chmod(self.directory / "runtime.lock", 0o600)
        (self.directory / "runtime.lock").unlink()
        (self.directory / "runtime.lock").symlink_to(target.name)
        with self.assertRaises(PrivateJSONLLedgerError):
            PrivateJSONLLedger.open(self.directory, GENESIS)
        (self.directory / "runtime.lock").unlink()
        (self.directory / "runtime.lock").write_bytes(b"")
        os.chmod(self.directory / "runtime.lock", 0o600)
        os.link(self.directory / "runtime.jsonl", self.directory / "ledger-alias")
        with self.assertRaises(PrivateJSONLLedgerError):
            PrivateJSONLLedger.open(self.directory, GENESIS)
        (self.directory / "ledger-alias").unlink()
        os.link(self.directory / "runtime.lock", self.directory / "lock-alias")
        with self.assertRaises(PrivateJSONLLedgerError):
            PrivateJSONLLedger.open(self.directory, GENESIS)

    def test_detects_ledger_path_replacement_before_append(self):
        ledger = PrivateJSONLLedger.open(self.directory, GENESIS)
        try:
            replacement = self.directory / "replacement"
            replacement.write_bytes(b"")
            os.chmod(replacement, 0o600)
            os.replace(replacement, self.directory / "runtime.jsonl")
            with self.assertRaises(PrivateJSONLLedgerError):
                ledger.append(record(GENESIS, 1))
        finally:
            ledger.close()

    def test_second_process_cannot_acquire_nonblocking_lock(self):
        ledger = PrivateJSONLLedger.open(self.directory, GENESIS)
        try:
            program = (
                "from pathlib import Path\n"
                "from scripts.live_eval.private_jsonl_ledger import PrivateJSONLLedger\n"
                "try:\n"
                " PrivateJSONLLedger.open(Path(sys.argv[1]), sys.argv[2])\n"
                "except Exception:\n"
                " raise SystemExit(0)\n"
                "raise SystemExit(1)\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", "import sys;" + program, str(self.directory), GENESIS],
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            )
            self.assertEqual(result.returncode, 0)
        finally:
            ledger.close()

    def test_creation_and_append_fsync(self):
        with mock.patch("scripts.live_eval.private_jsonl_ledger.os.fsync", wraps=os.fsync) as synced:
            with PrivateJSONLLedger.open(self.directory, GENESIS) as ledger:
                directory_fd = ledger._directory_fd
                ledger_fd = ledger._ledger_fd
                self.assertIn(mock.call(directory_fd), synced.call_args_list)
                ledger.append(record(GENESIS, 1))
                self.assertIn(mock.call(ledger_fd), synced.call_args_list)


if __name__ == "__main__":
    unittest.main()

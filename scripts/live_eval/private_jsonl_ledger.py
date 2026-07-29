"""Private, cooperative, append-only canonical JSONL ledger."""

import errno
import fcntl
import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Optional, Tuple, Union

from scripts.workflow_coordination.canonical_json import (
    CanonicalJSONError,
    canonical_bytes,
    load_canonical_input,
)


class PrivateJSONLLedgerError(RuntimeError):
    """Raised when a private ledger cannot be trusted or used."""


@dataclass(frozen=True)
class PrivateJSONLLedgerState:
    record_count: int
    total_bytes: int
    last_record_hash: str


class PrivateJSONLLedger:
    """A process-cooperative private canonical JSONL ledger."""

    _LEDGER_NAME = "runtime.jsonl"
    _LOCK_NAME = "runtime.lock"
    _DIGEST_PREFIX = "sha256:"
    _DIGEST_LENGTH = len(_DIGEST_PREFIX) + 64

    def __init__(
        self,
        directory_fd: int,
        ledger_fd: int,
        lock_fd: int,
        directory_identity: Tuple[int, int],
        ledger_identity: Tuple[int, int],
        lock_identity: Tuple[int, int],
        state: PrivateJSONLLedgerState,
        max_records: int,
        max_bytes: int,
        max_record_bytes: int,
    ) -> None:
        self._directory_fd = directory_fd
        self._ledger_fd = ledger_fd
        self._lock_fd = lock_fd
        self._directory_identity = directory_identity
        self._ledger_identity = ledger_identity
        self._lock_identity = lock_identity
        self._state = state
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._max_record_bytes = max_record_bytes
        self._closed = False
        self._poisoned = False

    @property
    def state(self) -> PrivateJSONLLedgerState:
        return self._state

    @classmethod
    def open(
        cls,
        directory: Union[str, os.PathLike],
        genesis_digest: str,
        *,
        max_records: int = 64,
        max_bytes: int = 262144,
        max_record_bytes: int = 16384,
    ) -> "PrivateJSONLLedger":
        cls._validate_limits(max_records, max_bytes, max_record_bytes)
        cls._validate_digest(genesis_digest)
        directory_fd = ledger_fd = lock_fd = -1
        created = []
        try:
            directory_fd = cls._open_directory(directory)
            directory_info = cls._validate_directory_fd(directory_fd)
            ledger_exists = cls._name_exists(directory_fd, cls._LEDGER_NAME)
            lock_exists = cls._name_exists(directory_fd, cls._LOCK_NAME)
            if ledger_exists != lock_exists:
                raise PrivateJSONLLedgerError("mixed ledger file state")
            if ledger_exists:
                ledger_fd = cls._open_existing_file(directory_fd, cls._LEDGER_NAME, True)
                lock_fd = cls._open_existing_file(directory_fd, cls._LOCK_NAME, False)
            else:
                ledger_fd = cls._create_file(directory_fd, cls._LEDGER_NAME)
                created.append((cls._LEDGER_NAME, os.fstat(ledger_fd)))
                lock_fd = cls._create_file(directory_fd, cls._LOCK_NAME)
                created.append((cls._LOCK_NAME, os.fstat(lock_fd)))
                os.fsync(directory_fd)
            ledger_info = cls._validate_file_fd(ledger_fd)
            lock_info = cls._validate_file_fd(lock_fd)
            cls._lock(lock_fd)
            state = cls._read_history(
                ledger_fd, genesis_digest, max_records, max_bytes, max_record_bytes
            )
            return cls(
                directory_fd,
                ledger_fd,
                lock_fd,
                cls._identity(directory_info),
                cls._identity(ledger_info),
                cls._identity(lock_info),
                state,
                max_records,
                max_bytes,
                max_record_bytes,
            )
        except (CanonicalJSONError, OSError, ValueError) as error:
            cls._cleanup_created(directory_fd, created)
            cls._close_fd(lock_fd)
            cls._close_fd(ledger_fd)
            cls._close_fd(directory_fd)
            if isinstance(error, PrivateJSONLLedgerError):
                raise
            raise PrivateJSONLLedgerError("cannot open private JSONL ledger") from error
        except PrivateJSONLLedgerError:
            cls._cleanup_created(directory_fd, created)
            cls._close_fd(lock_fd)
            cls._close_fd(ledger_fd)
            cls._close_fd(directory_fd)
            raise

    def __enter__(self) -> "PrivateJSONLLedger":
        self._require_usable()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_fd(self._lock_fd)
        self._close_fd(self._ledger_fd)
        self._close_fd(self._directory_fd)

    def append(self, record_bytes: bytes) -> PrivateJSONLLedgerState:
        self._require_usable()
        self._validate_append_record(record_bytes)
        line = record_bytes + b"\n"
        if self._state.record_count >= self._max_records:
            raise PrivateJSONLLedgerError("record count limit exceeded")
        if len(record_bytes) > self._max_record_bytes:
            raise PrivateJSONLLedgerError("record byte limit exceeded")
        if self._state.total_bytes + len(line) > self._max_bytes:
            raise PrivateJSONLLedgerError("ledger byte limit exceeded")
        self._validate_retained_identities()
        try:
            self._write_all(line)
            os.fsync(self._ledger_fd)
        except OSError as error:
            self._poisoned = True
            self.close()
            raise PrivateJSONLLedgerError("ledger append failed; object is closed") from error
        try:
            self._validate_retained_identities()
        except PrivateJSONLLedgerError:
            self._poisoned = True
            self.close()
            raise
        self._state = PrivateJSONLLedgerState(
            self._state.record_count + 1,
            self._state.total_bytes + len(line),
            self._hash(record_bytes),
        )
        return self._state

    @classmethod
    def _open_directory(cls, directory: Union[str, os.PathLike]) -> int:
        raw_path = os.fspath(directory)
        if not isinstance(raw_path, str) or not os.path.isabs(raw_path):
            raise PrivateJSONLLedgerError("ledger directory must be an absolute path")
        normalized = os.path.normpath(raw_path)
        if raw_path != normalized or os.path.realpath(raw_path) != raw_path:
            raise PrivateJSONLLedgerError("ledger directory path must be canonical")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        return os.open(normalized, flags)

    @classmethod
    def _validate_directory_fd(cls, descriptor: int) -> os.stat_result:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise PrivateJSONLLedgerError("ledger directory is not trusted")
        return info

    @classmethod
    def _create_file(cls, directory_fd: int, name: str) -> int:
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        return descriptor

    @classmethod
    def _open_existing_file(cls, directory_fd: int, name: str, append: bool) -> int:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if append:
            flags |= os.O_APPEND
        return os.open(name, flags, dir_fd=directory_fd)

    @classmethod
    def _validate_file_fd(cls, descriptor: int) -> os.stat_result:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise PrivateJSONLLedgerError("ledger file is not trusted")
        return info

    @classmethod
    def _name_exists(cls, directory_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    @classmethod
    def _lock(cls, descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            raise PrivateJSONLLedgerError("ledger lock is already held") from error

    @classmethod
    def _read_history(
        cls,
        descriptor: int,
        genesis_digest: str,
        max_records: int,
        max_bytes: int,
        max_record_bytes: int,
    ) -> PrivateJSONLLedgerState:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(8192, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PrivateJSONLLedgerError("ledger byte limit exceeded")
            chunks.append(chunk)
        data = b"".join(chunks)
        if data and not data.endswith(b"\n"):
            raise PrivateJSONLLedgerError("ledger has a partial trailing record")
        expected_hash = genesis_digest
        count = 0
        for raw_line in data[:-1].split(b"\n") if data else ():
            if len(raw_line) > max_record_bytes:
                raise PrivateJSONLLedgerError("record byte limit exceeded")
            if count >= max_records:
                raise PrivateJSONLLedgerError("record count limit exceeded")
            try:
                parsed = load_canonical_input(raw_line)
                encoded = canonical_bytes(parsed)
            except (CanonicalJSONError, RecursionError) as error:
                raise PrivateJSONLLedgerError("ledger record is invalid") from error
            if (
                encoded != raw_line
                or not isinstance(parsed, dict)
                or "previous_record_hash" not in parsed
                or parsed["previous_record_hash"] != expected_hash
            ):
                raise PrivateJSONLLedgerError("ledger record chain is invalid")
            expected_hash = cls._hash(raw_line)
            count += 1
        return PrivateJSONLLedgerState(count, total, expected_hash)

    def _validate_append_record(self, record_bytes: bytes) -> None:
        if type(record_bytes) is not bytes:
            raise PrivateJSONLLedgerError("record must be bytes")
        try:
            parsed = load_canonical_input(record_bytes)
            encoded = canonical_bytes(parsed)
        except (CanonicalJSONError, RecursionError) as error:
            raise PrivateJSONLLedgerError("record is not canonical JSON") from error
        if (
            encoded != record_bytes
            or not isinstance(parsed, dict)
            or "previous_record_hash" not in parsed
            or parsed["previous_record_hash"] != self._state.last_record_hash
        ):
            raise PrivateJSONLLedgerError("record chain is invalid")

    def _validate_retained_identities(self) -> None:
        self._validate_directory_fd(self._directory_fd)
        self._validate_identity(self._ledger_fd, self._ledger_identity, self._LEDGER_NAME)
        self._validate_identity(self._lock_fd, self._lock_identity, self._LOCK_NAME)

    def _validate_identity(self, descriptor: int, identity: Tuple[int, int], name: str) -> None:
        retained = self._validate_file_fd(descriptor)
        try:
            current = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except OSError as error:
            raise PrivateJSONLLedgerError("ledger path is unavailable") from error
        self._validate_file_metadata(current)
        if self._identity(retained) != identity or self._identity(current) != identity:
            raise PrivateJSONLLedgerError("ledger path identity changed")

    @classmethod
    def _validate_file_metadata(cls, info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise PrivateJSONLLedgerError("ledger file is not trusted")

    @classmethod
    def _validate_digest(cls, value: str) -> None:
        if (
            type(value) is not str
            or len(value) != cls._DIGEST_LENGTH
            or not value.startswith(cls._DIGEST_PREFIX)
            or any(character not in "0123456789abcdef" for character in value[7:])
        ):
            raise PrivateJSONLLedgerError("invalid genesis digest")

    @staticmethod
    def _validate_limits(*values: int) -> None:
        if any(type(value) is not int or value < 1 for value in values):
            raise PrivateJSONLLedgerError("ledger limits must be positive integers")

    @staticmethod
    def _identity(info: os.stat_result) -> Tuple[int, int]:
        return (info.st_dev, info.st_ino)

    @classmethod
    def _hash(cls, raw_line: bytes) -> str:
        return cls._DIGEST_PREFIX + hashlib.sha256(raw_line).hexdigest()

    def _write_all(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = os.write(self._ledger_fd, data[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "incomplete ledger write")
            offset += written

    def _require_usable(self) -> None:
        if self._closed or self._poisoned:
            raise PrivateJSONLLedgerError("ledger is closed")

    @staticmethod
    def _close_fd(descriptor: int) -> None:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass

    @classmethod
    def _cleanup_created(cls, directory_fd: int, created: list) -> None:
        if directory_fd < 0:
            return
        for name, created_info in created:
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if cls._identity(current) == cls._identity(created_info):
                    os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass

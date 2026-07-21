"""Fail-closed retention of redacted live-evaluation artifacts."""

import codecs
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable, Mapping, Optional, Union


class RedactionError(RuntimeError):
    """Raised when raw input cannot be safely retained."""


class _LiteralRedactor:
    _HOME_PATH = re.compile(r"/(?:home|Users)/[^\s\"']+")

    def __init__(self, secrets: Mapping[str, str]) -> None:
        if not isinstance(secrets, Mapping):
            raise TypeError("secrets must be a mapping of strings")
        literals = []
        for name, value in secrets.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError("secret names and values must be strings")
            for secret in (name, value):
                if secret:
                    literals.append(secret)
                    escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
                    if escaped:
                        literals.append(escaped)
        home = str(Path.home())
        if home:
            literals.append(home)
        self._literals = tuple(sorted(set(literals), key=len, reverse=True))

    def redact(self, text: str) -> str:
        for literal in self._literals:
            text = text.replace(literal, "[REDACTED]")
        return self._HOME_PATH.sub("[REDACTED_HOME]", text)


class RedactingWriter:
    """Buffers bounded raw bytes and retains only finalized, redacted bytes."""

    DEFAULT_ARTIFACT_NAME = "live-eval.jsonl"
    DEFAULT_MAX_RAW_BYTES = 1024 * 1024

    def __init__(
        self,
        directory: Union[str, os.PathLike],
        secrets: Mapping[str, str],
        *,
        artifact_name: str = DEFAULT_ARTIFACT_NAME,
        max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES,
        redactor: Optional[Union[Callable[[str], str], object]] = None,
    ) -> None:
        self._directory = Path(directory)
        self._validate_directory(self._directory)
        if (
            not isinstance(artifact_name, str)
            or not artifact_name
            or Path(artifact_name).name != artifact_name
            or artifact_name in (".", "..")
        ):
            raise RedactionError("artifact_name must be a single file name")
        if isinstance(max_raw_bytes, bool) or not isinstance(max_raw_bytes, int) or max_raw_bytes <= 0:
            raise ValueError("max_raw_bytes must be a positive integer")

        self.path = self._directory / artifact_name
        self._max_raw_bytes = max_raw_bytes
        self._redactor = redactor if redactor is not None else _LiteralRedactor(secrets)
        self._buffer = bytearray()
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._closed = False
        self._retained_path = None  # type: Optional[Path]

    @staticmethod
    def _validate_directory(directory: Path) -> None:
        try:
            info = directory.lstat()
        except OSError as error:
            raise RedactionError("artifact directory is unavailable") from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise RedactionError("artifact directory must be an owned, non-symlink mode-0700 directory")

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def retained_path(self) -> Optional[Path]:
        return self._retained_path

    def _discard_raw(self) -> None:
        if self._buffer:
            self._buffer[:] = b"\x00" * len(self._buffer)
            self._buffer.clear()

    def _fail(self, message: str, cause: Optional[BaseException] = None) -> None:
        self._discard_raw()
        self._closed = True
        error = RedactionError(message)
        if cause is None:
            raise error
        raise error from cause

    def write(self, chunk: bytes) -> None:
        if self._closed:
            raise RedactionError("writer is closed")
        if not isinstance(chunk, bytes):
            self._fail("chunk must be bytes")
        if len(self._buffer) + len(chunk) > self._max_raw_bytes:
            self._fail("raw artifact exceeds max_raw_bytes")
        try:
            self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as error:
            self._fail("artifact is not valid UTF-8", error)
        self._buffer.extend(chunk)

    def _redact(self, text: str) -> str:
        try:
            method = getattr(self._redactor, "redact", None)
            retained = method(text) if callable(method) else self._redactor(text)
        except Exception as error:
            self._fail("artifact redaction failed", error)
        if not isinstance(retained, str):
            self._fail("artifact redaction returned invalid data")
        return retained

    @staticmethod
    def _unlink_if_identity(path: Path, identity: tuple) -> None:
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == identity:
                path.unlink()
        except OSError:
            pass

    def finalize(self) -> Path:
        if self._closed:
            raise RedactionError("writer is closed")
        try:
            self._decoder.decode(b"", final=True)
            raw_text = bytes(self._buffer).decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            self._fail("artifact is not valid UTF-8", error)

        retained_bytes = self._redact(raw_text).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = None
        identity = None
        try:
            fd = os.open(str(self.path), flags, 0o600)
            opened = os.fstat(fd)
            identity = (opened.st_dev, opened.st_ino)
            offset = 0
            while offset < len(retained_bytes):
                written = os.write(fd, retained_bytes[offset:])
                if written <= 0:
                    raise OSError("artifact write made no progress")
                offset += written
            os.fsync(fd)
            os.close(fd)
            fd = None
        except Exception as error:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if identity is not None:
                self._unlink_if_identity(self.path, identity)
            self._fail("artifact retention failed", error)

        self._discard_raw()
        self._closed = True
        self._retained_path = self.path
        return self.path

"""Fail-closed retention of redacted live-evaluation artifacts."""

import codecs
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Callable, Dict, Mapping, Optional, Tuple, Union


class RedactionError(RuntimeError):
    """Raised when raw input cannot be safely retained."""


_PathToken = Tuple[int, int]


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


class _LiteralRedactor:
    _HOME_PATH = re.compile(r"/(?:home|Users)/[^\s\"']+")

    def __init__(self, configured_secrets: Mapping[str, str]) -> None:
        if not isinstance(configured_secrets, Mapping):
            raise TypeError("secrets must be a mapping of strings")
        literals = []
        for name, value in configured_secrets.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError("secret names and values must be strings")
            literals.extend(secret for secret in (name, value) if secret)
        home = str(Path.home())
        if home:
            literals.append(home)
        self._literals = tuple(sorted(set(literals), key=len, reverse=True))

    def _redact_string(self, value: str) -> str:
        for literal in self._literals:
            value = value.replace(literal, "")
        return self._HOME_PATH.sub("", value)

    def redact(self, value: object) -> object:
        if isinstance(value, str):
            return self._redact_string(value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            redacted = {}  # type: Dict[str, object]
            for key, item in value.items():
                redacted_key = self._redact_string(key)
                if redacted_key in redacted:
                    raise ValueError("redacted object keys collide")
                redacted[redacted_key] = self.redact(item)
            return redacted
        return value

    def require_clean(self, value: object) -> None:
        if isinstance(value, str):
            if any(literal in value for literal in self._literals):
                raise ValueError("configured secret remains after redaction")
            if self._HOME_PATH.search(value):
                raise ValueError("home path remains after redaction")
            return
        if isinstance(value, list):
            for item in value:
                self.require_clean(item)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                self.require_clean(key)
                self.require_clean(item)

    def require_serialized_clean(self, content: bytes) -> None:
        for literal in self._literals:
            if literal.encode("utf-8") in content:
                raise ValueError("configured secret remains in serialized artifact")


class RedactingWriter:
    """Buffers bounded raw JSONL and atomically retains only redacted bytes."""

    DEFAULT_ARTIFACT_NAME = "live-eval.jsonl"
    DEFAULT_MAX_RAW_BYTES = 1024 * 1024
    _STAGING_PREFIX = ".live-eval-stage-"

    def __init__(
        self,
        directory: Union[str, os.PathLike],
        configured_secrets: Mapping[str, str],
        *,
        artifact_name: str = DEFAULT_ARTIFACT_NAME,
        max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES,
        redactor: Optional[Union[Callable[[str], str], object]] = None,
    ) -> None:
        if (
            not isinstance(artifact_name, str)
            or not artifact_name
            or Path(artifact_name).name != artifact_name
            or artifact_name in (".", "..")
        ):
            raise RedactionError("artifact_name must be a single file name")
        if isinstance(max_raw_bytes, bool) or not isinstance(max_raw_bytes, int) or max_raw_bytes <= 0:
            raise ValueError("max_raw_bytes must be a positive integer")

        self._directory = Path(directory)
        self.path = self._directory / artifact_name
        self._artifact_name = artifact_name
        self._max_raw_bytes = max_raw_bytes
        self._custom_redactor = redactor
        self._literal_redactor = _LiteralRedactor(configured_secrets)
        self._buffer = bytearray()
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._closed = False
        self._retained_path = None  # type: Optional[Path]
        self._cleanup_warnings = []  # type: list
        self._directory_fd = None  # type: Optional[int]
        self._directory_token = None  # type: Optional[_PathToken]
        self._open_trusted_directory()

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    @staticmethod
    def _valid_directory(info: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o700
            and info.st_uid == os.getuid()
        )

    def _open_trusted_directory(self) -> None:
        fd = None
        try:
            fd = os.open(str(self._directory), self._directory_flags())
            info = os.fstat(fd)
            if not self._valid_directory(info):
                raise OSError("untrusted artifact directory")
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise RedactionError(
                "artifact directory must be an owned, non-symlink mode-0700 directory"
            ) from None
        self._directory_fd = fd
        self._directory_token = (info.st_dev, info.st_ino)

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
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")

    def _close_directory(self) -> None:
        fd = self._directory_fd
        self._directory_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _fail(self, message: str) -> None:
        self._discard_raw()
        self._closed = True
        self._close_directory()
        error = RedactionError(message)
        error.cleanup_warnings = tuple(self._cleanup_warnings)
        raise error from None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._discard_raw()
        self._close_directory()

    def abort(self) -> None:
        self.close()

    def __enter__(self) -> "RedactingWriter":
        if self._closed:
            raise RedactionError("writer is closed")
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def write(self, chunk: bytes) -> None:
        if self._closed:
            raise RedactionError("writer is closed")
        if not isinstance(chunk, bytes):
            self._fail("chunk must be bytes")
        if len(self._buffer) + len(chunk) > self._max_raw_bytes:
            self._fail("raw artifact exceeds max_raw_bytes")
        try:
            self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            self._fail("artifact is not valid UTF-8")
        self._buffer.extend(chunk)

    def _parse_jsonl(self) -> list:
        try:
            self._decoder.decode(b"", final=True)
            text = self._buffer.decode("utf-8", "strict")
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            if any(not line.strip() for line in lines):
                raise ValueError("blank JSONL line")
            return [
                json.loads(line, parse_constant=_reject_json_constant)
                for line in lines
            ]
        except (UnicodeDecodeError, ValueError, TypeError):
            self._fail("artifact is not valid UTF-8 JSONL")
        raise AssertionError("unreachable")

    def _redact_record(self, value: object) -> object:
        try:
            candidate = value
            if self._custom_redactor is not None:
                text = _compact_json(value)
                method = getattr(self._custom_redactor, "redact", None)
                retained = method(text) if callable(method) else self._custom_redactor(text)
                if not isinstance(retained, str):
                    raise TypeError("custom redactor must return JSON text")
                candidate = json.loads(retained, parse_constant=_reject_json_constant)
            redacted = self._literal_redactor.redact(candidate)
            self._literal_redactor.require_clean(redacted)
            return redacted
        except Exception:
            self._fail("artifact redaction failed")
        raise AssertionError("unreachable")

    def _retained_bytes(self) -> bytes:
        records = self._parse_jsonl()
        try:
            lines = [_compact_json(self._redact_record(record)) for record in records]
            retained = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
            self._literal_redactor.require_serialized_clean(retained)
            return retained
        except (TypeError, ValueError, UnicodeEncodeError):
            self._fail("artifact redaction returned invalid data")
        raise AssertionError("unreachable")

    def _verify_directory_identity(self) -> None:
        current_fd = None
        try:
            if self._directory_fd is None or self._directory_token is None:
                raise OSError("closed directory")
            opened = os.fstat(self._directory_fd)
            if not self._valid_directory(opened):
                raise OSError("directory trust changed")
            if (opened.st_dev, opened.st_ino) != self._directory_token:
                raise OSError("directory identity changed")
            current_fd = os.open(str(self._directory), self._directory_flags())
            current = os.fstat(current_fd)
            if not self._valid_directory(current):
                raise OSError("directory trust changed")
            if (current.st_dev, current.st_ino) != self._directory_token:
                raise OSError("directory path changed")
        finally:
            if current_fd is not None:
                try:
                    os.close(current_fd)
                except OSError:
                    pass

    def _recheck_directory(self) -> None:
        try:
            self._verify_directory_identity()
        except Exception:
            self._fail("artifact directory identity changed")

    def _token_for_name(self, name: str) -> Optional[_PathToken]:
        try:
            info = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
        except OSError:
            return None
        return (info.st_dev, info.st_ino)

    def _unlink_if_token(self, name: str, token: Optional[_PathToken]) -> None:
        if token is None or self._directory_fd is None:
            return
        try:
            if self._token_for_name(name) == token:
                os.unlink(name, dir_fd=self._directory_fd)
        except OSError:
            self._cleanup_warnings.append("could not remove owned artifact path")

    def _unlink_owned_required(self, name: str, token: Optional[_PathToken]) -> None:
        if token is not None and self._token_for_name(name) == token:
            os.unlink(name, dir_fd=self._directory_fd)

    def _open_staging(self) -> Tuple[int, str, _PathToken]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        for _attempt in range(16):
            name = self._STAGING_PREFIX + secrets.token_hex(16)
            fd = None
            created = False
            token = None  # type: Optional[_PathToken]
            try:
                fd = os.open(name, flags, 0o600, dir_fd=self._directory_fd)
                created = True
            except FileExistsError:
                continue
            try:
                info = os.fstat(fd)
                token = (info.st_dev, info.st_ino)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("staging artifact is not regular")
                os.fchmod(fd, 0o600)
                return fd, name, token
            except Exception:
                if created:
                    if token is None:
                        try:
                            os.unlink(name, dir_fd=self._directory_fd)
                        except OSError:
                            self._cleanup_warnings.append(
                                "could not remove created staging artifact"
                            )
                    else:
                        self._unlink_if_token(name, token)
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        raise OSError("could not allocate staging artifact")

    @staticmethod
    def _write_all(fd: int, content: bytes) -> None:
        offset = 0
        while offset < len(content):
            written = os.write(fd, content[offset:])
            if written <= 0:
                raise OSError("artifact write made no progress")
            offset += written

    def finalize(self) -> Path:
        if self._closed:
            raise RedactionError("writer is closed")
        retained_bytes = self._retained_bytes()
        self._recheck_directory()

        staging_fd = None
        staging_name = None  # type: Optional[str]
        token = None  # type: Optional[_PathToken]
        published = False
        try:
            staging_fd, staging_name, token = self._open_staging()
            self._write_all(staging_fd, retained_bytes)
            os.fsync(staging_fd)
            os.close(staging_fd)
            staging_fd = None
            self._verify_directory_identity()
            os.link(
                staging_name,
                self._artifact_name,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            published = True
            os.fsync(self._directory_fd)
            self._verify_directory_identity()
            self._unlink_owned_required(staging_name, token)
            os.fsync(self._directory_fd)
        except Exception:
            if staging_fd is not None:
                try:
                    os.close(staging_fd)
                except OSError:
                    pass
            if published:
                self._unlink_if_token(self._artifact_name, token)
            if staging_name is not None:
                self._unlink_if_token(staging_name, token)
            self._fail("artifact retention failed")

        self._discard_raw()
        self._closed = True
        self._retained_path = self.path
        self._close_directory()
        return self.path

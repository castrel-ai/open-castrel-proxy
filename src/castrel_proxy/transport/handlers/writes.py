"""Write and edit support for ``files.v1``: chunked atomic writes and text edits.

A chunked write lands in ``<path>.castrel-tmp-<request_id>`` and is moved into place with
``os.replace`` only when the declared ``total_bytes`` has been written, so a dropped connection
never leaves a half-written file. One writer per path at a time; a second concurrent write is
rejected instead of interleaving bytes.
"""

from __future__ import annotations

import base64
import os
import threading
from pathlib import Path
from typing import Any

from ...operations import paths
from ...security.policy import FilesPolicy

UTF8 = "utf-8"
BASE64 = "base64"


class WriteState:
    """Temporary files and per-path locks shared by the write handlers."""

    def __init__(self) -> None:
        self._temp_files: dict[str, Path] = {}
        self._request_paths: dict[str, str] = {}
        self._inflight_paths: set[str] = set()
        self._guard = threading.Lock()

    def acquire(self, key: str, request_id: str) -> None:
        with self._guard:
            if key in self._inflight_paths:
                raise paths.FilesError(
                    "invalid_request", "Another write to this path is in progress."
                )
            self._inflight_paths.add(key)
            if request_id:
                self._request_paths[request_id] = key

    def release(self, key: str) -> None:
        with self._guard:
            self._inflight_paths.discard(key)

    def discard(self, key: str) -> None:
        with self._guard:
            temp = self._temp_files.pop(key, None)
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def temp_for(self, key: str) -> Path | None:
        with self._guard:
            return self._temp_files.get(key)

    def remember_temp(self, key: str, temp: Path) -> None:
        with self._guard:
            self._temp_files[key] = temp

    def forget_temp(self, key: str) -> None:
        with self._guard:
            self._temp_files.pop(key, None)

    def cancel(self, request_id: str) -> None:
        """Remove the temporary file of a cancelled or timed-out write."""
        with self._guard:
            key = self._request_paths.pop(request_id, None)
        if key:
            self.discard(key)


def decode_content(content: str, encoding: str) -> bytes:
    if encoding == BASE64:
        try:
            return base64.b64decode(content.encode("ascii"), validate=True)
        except ValueError as error:
            raise paths.FilesError("bad_encoding", "Content is not valid base64.") from error
    return content.encode(UTF8)


def ensure_parent(path: Path, create_dirs: bool) -> None:
    parent = path.parent
    if parent.exists():
        return
    if create_dirs:
        parent.mkdir(parents=True, exist_ok=True)
        return
    raise paths.FilesError("not_found", "Parent directory does not exist.")


def non_negative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_name(f"{path.name}.castrel-tmp-edit")
    temp.write_bytes(data)
    os.replace(temp, path)


def write_file(
    state: WriteState, path: Path, data: dict[str, Any], policy: FilesPolicy
) -> dict[str, Any]:
    """Write one chunk; the last chunk replaces the target atomically."""
    mode = str(data.get("mode") or "overwrite")
    if mode not in ("overwrite", "append", "create_new"):
        raise paths.FilesError("invalid_request", "Unsupported write mode.")
    chunk = decode_content(str(data.get("content") or ""), str(data.get("encoding") or UTF8))
    request_id = str(data.get("request_id") or "")
    key = str(path)
    ensure_parent(path, bool(data.get("create_dirs", True)))
    state.acquire(key, request_id)
    try:
        if mode == "append" and state.temp_for(key) is None:
            # Plain append to an existing file; a chunked write in progress keeps using its
            # temporary file even when the continuation arrives with mode="append".
            written = _append(path, chunk, data.get("offset"))
            if written > policy.max_file_bytes:
                raise paths.FilesError("too_large", "The file would exceed the node limit.")
        else:
            written = _write_chunk(state, path, key, chunk, mode, data, policy, request_id)
    except BaseException:
        # A failed chunk must not leave a half-written temporary file behind.
        state.discard(key)
        raise
    finally:
        state.release(key)
    return {
        "path": key,
        "size": path.stat().st_size if path.exists() else written,
        "bytes_written": len(chunk),
    }


def _append(path: Path, chunk: bytes, offset: Any) -> int:
    if not path.exists():
        if non_negative_int(offset) != 0:
            raise paths.FilesError("not_found", "File does not exist.")
        path.write_bytes(b"")
    current = path.stat().st_size
    start = non_negative_int(offset)
    if start != 0 and start != current:
        raise paths.FilesError("invalid_request", "Offset does not match the current file size.")
    with path.open("ab") as handle:
        handle.write(chunk)
    return current + len(chunk)


def _write_chunk(
    state: WriteState,
    path: Path,
    key: str,
    chunk: bytes,
    mode: str,
    data: dict[str, Any],
    policy: FilesPolicy,
    request_id: str,
) -> int:
    if mode == "create_new" and path.exists():
        raise paths.FilesError("invalid_request", "File already exists.")
    start = non_negative_int(data.get("offset"))
    temp = state.temp_for(key)
    if start == 0:
        if temp is not None:
            state.discard(key)
        temp = path.with_name(f"{path.name}.castrel-tmp-{request_id or os.getpid()}")
        temp.write_bytes(chunk)
        state.remember_temp(key, temp)
        written = len(chunk)
    else:
        if temp is None or not temp.exists():
            raise paths.FilesError(
                "invalid_request", "No chunked write is in progress; restart with offset=0."
            )
        written = temp.stat().st_size
        if start != written:
            raise paths.FilesError(
                "invalid_request", "Offset does not match the bytes already written."
            )
        with temp.open("ab") as handle:
            handle.write(chunk)
        written += len(chunk)
    if written > policy.max_file_bytes:
        raise paths.FilesError("too_large", "The file would exceed the node limit.")
    total = data.get("total_bytes")
    if total is None or written >= non_negative_int(total):
        os.replace(temp, path)
        state.forget_temp(key)
    return written


def edit_file(path: Path, data: dict[str, Any], policy: FilesPolicy) -> dict[str, Any]:
    """Apply one text edit (replace / append / prepend) atomically."""
    if not path.exists():
        raise paths.FilesError("not_found", "File does not exist.")
    if path.is_dir():
        raise paths.FilesError("is_a_directory", "Path is a directory, not a file.")
    if path.stat().st_size > policy.max_file_bytes:
        raise paths.FilesError("too_large", "File exceeds the node limit.")
    try:
        text = path.read_text(encoding=UTF8)
    except UnicodeDecodeError as error:
        raise paths.FilesError("bad_encoding", "Editing requires a UTF-8 text file.") from error
    operation = str(data.get("operation") or "replace")
    new_content = str(data.get("new_content") or "")
    if operation == "replace":
        old_content = data.get("old_content")
        if not isinstance(old_content, str) or not old_content:
            raise paths.FilesError("invalid_request", "Replace requires old_content.")
        replaced = text.count(old_content)
        updated = text.replace(old_content, new_content)
    elif operation == "append":
        replaced = 1 if new_content else 0
        updated = text + new_content
    elif operation == "prepend":
        replaced = 1 if new_content else 0
        updated = new_content + text
    else:
        raise paths.FilesError("invalid_request", "Unsupported edit operation.")
    encoded = updated.encode(UTF8)
    if len(encoded) > policy.max_file_bytes:
        raise paths.FilesError("too_large", "The edited file would exceed the node limit.")
    if replaced:
        atomic_write(path, encoded)
    return {
        "path": str(path),
        "size": len(encoded),
        "operation": operation,
        "replaced": replaced,
    }


__all__ = [
    "BASE64",
    "UTF8",
    "WriteState",
    "atomic_write",
    "decode_content",
    "edit_file",
    "ensure_parent",
    "non_negative_int",
    "write_file",
]

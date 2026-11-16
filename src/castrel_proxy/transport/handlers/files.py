"""``files.v1`` handlers: paginated reads, directory listing and file-name search.

Every handler returns the payload of a ``<type>.result`` frame; failures carry an enum error
code instead of raising, so the platform always gets an answer for the request it sent.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...operations import paths
from ...security.policy import FilesPolicy, ProxyPolicy
from .writes import WriteState, edit_file, write_file

UTF8 = "utf-8"
BASE64 = "base64"
DEFAULT_LIST_LIMIT = 100
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_SEARCH_DEPTH = 4


class FilesHandler:
    """Serve ``node.fs.read`` / ``list`` / ``search`` against the node filesystem."""

    def __init__(self, policy_getter: Callable[[], ProxyPolicy]) -> None:
        self._policy_getter = policy_getter
        # Chunked writes keep their temporary files and per-path locks in this state.
        self._writes = WriteState()

    @property
    def policy(self) -> FilesPolicy:
        return self._policy_getter().files

    async def handle(self, message_type: str, data: dict[str, Any]) -> dict[str, Any]:
        request_id = str(data.get("request_id") or "")
        started = time.monotonic()
        try:
            if message_type == "node.fs.read":
                payload = await asyncio.to_thread(self._read, data)
            elif message_type == "node.fs.write":
                payload = await asyncio.to_thread(self._write, data)
            elif message_type == "node.fs.edit":
                payload = await asyncio.to_thread(self._edit, data)
            elif message_type == "node.fs.list":
                payload = await asyncio.to_thread(self._list, data)
            elif message_type == "node.fs.search":
                payload = await asyncio.to_thread(self._search, data)
            else:
                raise paths.FilesError(
                    "unknown_message_type", f"Unsupported message type: {message_type}"
                )
        except paths.FilesError as error:
            return _failure(request_id, error.code, str(error), started)
        except OSError as error:
            return _failure(request_id, "io_error", f"Filesystem error: {error}", started)
        return _success(request_id, payload, started)

    def _read(self, data: dict[str, Any]) -> dict[str, Any]:
        policy = self.policy
        path = paths.resolve(str(data.get("path") or ""), policy)
        if not path.exists():
            raise paths.FilesError("not_found", "File does not exist.")
        if path.is_dir():
            raise paths.FilesError("is_a_directory", "Path is a directory, not a file.")
        size = path.stat().st_size
        if size > policy.max_file_bytes:
            raise paths.FilesError(
                "too_large",
                f"File is {size} bytes; the node limit is {policy.max_file_bytes} bytes.",
            )
        offset = _non_negative_int(data.get("offset"))
        requested = _non_negative_int(data.get("max_bytes")) or policy.max_read_bytes
        max_bytes = max(1, min(requested, policy.max_read_bytes))
        if offset > size:
            raise paths.FilesError("invalid_request", "Offset is past the end of the file.")
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(max_bytes)
        content, encoding = _encode_chunk(chunk, str(data.get("encoding") or ""))
        next_offset = offset + len(chunk)
        return {
            "path": str(path),
            "size": size,
            "offset": offset,
            "next_offset": next_offset,
            "eof": next_offset >= size,
            "content": content,
            "encoding": encoding,
            "truncated": next_offset < size,
        }

    def _write(self, data: dict[str, Any]) -> dict[str, Any]:
        path = paths.resolve(str(data.get("path") or ""), self.policy)
        return write_file(self._writes, path, data, self.policy)

    def _edit(self, data: dict[str, Any]) -> dict[str, Any]:
        path = paths.resolve(str(data.get("path") or ""), self.policy)
        return edit_file(path, data, self.policy)

    def cancel(self, request_id: str) -> None:
        """Remove the temporary file of a cancelled or timed-out write."""
        self._writes.cancel(request_id)

    def _list(self, data: dict[str, Any]) -> dict[str, Any]:
        policy = self.policy
        path = paths.resolve(str(data.get("path") or "~"), policy)
        if not path.exists():
            raise paths.FilesError("not_found", "Directory does not exist.")
        if not path.is_dir():
            raise paths.FilesError("invalid_request", "Path is not a directory.")
        limit = max(1, min(_non_negative_int(data.get("limit")) or DEFAULT_LIST_LIMIT, 1000))
        include_files = bool(data.get("include_files", True))
        entries: list[dict[str, Any]] = []
        truncated = False
        for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if not include_files and not is_dir:
                continue
            if len(entries) >= limit:
                truncated = True
                break
            entries.append(_entry(child, is_dir))
        return {"path": str(path), "entries": entries, "truncated": truncated}

    def _search(self, data: dict[str, Any]) -> dict[str, Any]:
        policy = self.policy
        keyword = str(data.get("keyword") or "").strip()
        if not keyword:
            raise paths.FilesError("invalid_request", "Keyword must not be empty.")
        start = paths.resolve(str(data.get("path") or "~"), policy)
        if not start.exists():
            raise paths.FilesError("not_found", "Search root does not exist.")
        if not start.is_dir():
            raise paths.FilesError("invalid_request", "Search root is not a directory.")
        limit = max(1, min(_non_negative_int(data.get("limit")) or DEFAULT_SEARCH_LIMIT, 1000))
        max_depth = max(1, min(_non_negative_int(data.get("max_depth")) or DEFAULT_SEARCH_DEPTH, 8))
        include_hidden = bool(data.get("include_hidden", False))
        needle = keyword.lower()

        entries: list[dict[str, Any]] = []
        truncated = False
        queue: list[tuple[Path, int]] = [(start, 0)]
        while queue:
            current, depth = queue.pop(0)
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                continue
            for child in children:
                if not include_hidden and child.name.startswith("."):
                    continue
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                if needle in child.name.lower():
                    if len(entries) >= limit:
                        truncated = True
                        break
                    entries.append(_entry(child, is_dir))
                if is_dir and depth + 1 < max_depth:
                    queue.append((child, depth + 1))
            if truncated:
                break
        return {
            "keyword": keyword,
            "path": str(start),
            "max_depth": max_depth,
            "entries": entries,
            "truncated": truncated,
        }


def _encode_chunk(chunk: bytes, requested: str) -> tuple[str, str]:
    if requested == BASE64:
        return base64.b64encode(chunk).decode("ascii"), BASE64
    try:
        return chunk.decode(UTF8), UTF8
    except UnicodeDecodeError:
        # Binary content becomes base64 so the platform's string field validates; the next
        # page starts at ``next_offset``, so a split multi-byte character is re-read once.
        return base64.b64encode(chunk).decode("ascii"), BASE64


def _entry(path: Path, is_dir: bool) -> dict[str, Any]:
    size = 0
    modified_at: int | None = None
    try:
        stat = path.stat()
        size = 0 if is_dir else int(stat.st_size)
        modified_at = int(stat.st_mtime * 1000)
    except OSError:
        pass
    return {
        "name": path.name,
        "path": str(path),
        "is_dir": is_dir,
        "size": size,
        "modified_at": modified_at,
    }


def _non_negative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _success(request_id: str, payload: dict[str, Any], started: float) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "ok": True,
        "duration_ms": _elapsed(started),
        **payload,
    }


def _failure(request_id: str, code: str, message: str, started: float) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "ok": False,
        "error_code": code,
        "error": message,
        "duration_ms": _elapsed(started),
    }


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["FilesHandler"]

"""Path expansion and the filesystem boundary enforced for `files.v1`.

Resolution order matters: the policy denies after ``Path.resolve()``, so neither ``..`` nor a
symbolic link can escape the configured roots. Symlinks are refused before resolution unless
the policy allows them, which keeps a link inside a root from pointing at ``~/.ssh``.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..security.policy import FilesPolicy

# Kernel and device trees are never readable through the node, whatever the roots say.
DENIED_PREFIXES = ("/proc", "/sys", "/dev")


class FilesError(Exception):
    """A request the handler answers with an error code instead of a result payload."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve(raw: str, policy: FilesPolicy) -> Path:
    """Expand and validate one requested path, returning an absolute resolved path."""
    if not policy.enabled:
        raise FilesError("policy_denied", "Filesystem access is disabled by the node policy.")
    if not isinstance(raw, str) or not raw.strip():
        raise FilesError("invalid_request", "Path must not be empty.")
    if "\x00" in raw:
        raise FilesError("invalid_request", "Path must not contain NUL bytes.")

    expanded = os.path.expandvars(os.path.expanduser(raw.strip()))
    candidate = Path(os.path.normpath(expanded))
    if not candidate.is_absolute():
        candidate = Path(os.path.normpath(str(Path.cwd() / candidate)))
    try:
        resolved = candidate.resolve()
    except OSError as error:
        raise FilesError("io_error", f"Path cannot be resolved: {error}") from error

    for prefix in DENIED_PREFIXES:
        if _is_within(resolved, Path(prefix)):
            raise FilesError("path_denied", "System paths are not accessible.")
    if not policy.roots:
        raise FilesError("path_denied", "The node has no allowed filesystem roots.")

    matched: tuple[Path, Path] | None = None
    for item in policy.roots:
        raw_root = _absolute(item)
        if _is_within(resolved, _root(item)):
            matched = (raw_root, _root(item))
            break
    if matched is None:
        raise FilesError("path_denied", "Path is outside the allowed roots.")

    # Check links only below the matched root: a root that is itself a symlink is an explicit
    # policy decision (for example ``~`` on some systems), not a bypass.
    if not policy.allow_symlinks and _relative_has_symlink(candidate, matched[0]):
        raise FilesError("path_denied", "Symbolic links are not allowed by the node policy.")
    if any(_is_within(resolved, _root(item)) for item in policy.deny_paths):
        raise FilesError("path_denied", "Path is explicitly denied by the node policy.")
    return resolved


def _absolute(raw: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(os.path.normpath(expanded))
    return path if path.is_absolute() else Path(os.path.normpath(str(Path.cwd() / path)))


def _root(raw: str) -> Path:
    return _absolute(raw).resolve()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _relative_has_symlink(candidate: Path, raw_root: Path) -> bool:
    """Whether any component below ``raw_root`` on ``candidate`` is a symbolic link."""
    if not _is_within(candidate, raw_root) and candidate != raw_root:
        return False
    current = candidate
    while current != raw_root and current != current.parent:
        if os.path.islink(current):
            return True
        current = current.parent
    return False


__all__ = ["DENIED_PREFIXES", "FilesError", "resolve"]

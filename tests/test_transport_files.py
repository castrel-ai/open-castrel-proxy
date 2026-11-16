"""`files.v1` handlers: pagination, encoding fallback, path boundary and search."""

import asyncio
import base64
from pathlib import Path

import pytest
from castrel_proxy.pairing.keys import NodeKeys
from castrel_proxy.security.policy import parse_policy
from castrel_proxy.transport.ws import ProxyNodeRuntime


def runtime(root: Path, **filesystem) -> ProxyNodeRuntime:
    document = {
        "filesystem": {
            "roots": [str(root)],
            "deny_paths": [str(root / "denied")],
            **filesystem,
        }
    }
    node = ProxyNodeRuntime(
        server_url="https://castrel.test",
        keys=NodeKeys(node_id="2001", token="token", ed25519_private="", x25519_private=""),
    )
    node.policy = document
    node._policy = parse_policy(document)
    return node


def call(node: ProxyNodeRuntime, message_type: str, **data):
    return asyncio.run(node.files.handle(message_type, data))


def test_read_paginates_until_eof(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("0123456789", encoding="utf-8")
    node = runtime(tmp_path, max_read_bytes=4)

    first = call(node, "node.fs.read", request_id="r1", path=str(target))

    assert first["ok"] is True
    assert first["content"] == "0123"
    assert first["offset"] == 0
    assert first["next_offset"] == 4
    assert first["eof"] is False
    assert first["encoding"] == "utf-8"

    last = call(node, "node.fs.read", request_id="r2", path=str(target), offset=8, max_bytes=4)
    assert last["content"] == "89"
    assert last["eof"] is True
    assert last["truncated"] is False


def test_read_falls_back_to_base64_for_binary(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\xff\xfe\x00")
    node = runtime(tmp_path)

    result = call(node, "node.fs.read", request_id="r1", path=str(target))

    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == b"\xff\xfe\x00"


def test_read_rejects_an_offset_past_the_end(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("abc", encoding="utf-8")
    node = runtime(tmp_path)

    result = call(node, "node.fs.read", request_id="r1", path=str(target), offset=10)

    assert result["ok"] is False
    assert result["error_code"] == "invalid_request"


def test_read_rejects_a_file_over_the_limit(tmp_path):
    target = tmp_path / "big.md"
    target.write_text("x" * 20, encoding="utf-8")
    node = runtime(tmp_path, max_file_bytes=4)

    result = call(node, "node.fs.read", request_id="r1", path=str(target))

    assert result["ok"] is False
    assert result["error_code"] == "too_large"


def test_path_outside_the_roots_is_denied(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    node = runtime(tmp_path)

    result = call(node, "node.fs.read", request_id="r1", path=str(outside))

    assert result["ok"] is False
    assert result["error_code"] == "path_denied"


def test_deny_paths_win_over_roots(tmp_path):
    denied = tmp_path / "denied"
    denied.mkdir()
    target = denied / "keys.json"
    target.write_text("{}", encoding="utf-8")
    node = runtime(tmp_path)

    result = call(node, "node.fs.read", request_id="r1", path=str(target))

    assert result["ok"] is False
    assert result["error_code"] == "path_denied"


def test_symlinks_inside_a_root_are_refused(tmp_path):
    outside = tmp_path.parent / "linked.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    node = runtime(tmp_path)

    result = call(node, "node.fs.read", request_id="r1", path=str(link))

    assert result["ok"] is False
    assert result["error_code"] == "path_denied"


def test_list_returns_entries_without_roots(tmp_path):
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "a").mkdir()
    node = runtime(tmp_path)

    result = call(node, "node.fs.list", request_id="r1", path=str(tmp_path))

    assert result["ok"] is True
    assert "roots" not in result
    assert [entry["name"] for entry in result["entries"]] == ["a", "b.md"]
    assert result["entries"][0]["is_dir"] is True
    assert result["truncated"] is False


def test_list_can_skip_files(tmp_path):
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "a").mkdir()
    node = runtime(tmp_path)

    result = call(node, "node.fs.list", request_id="r1", path=str(tmp_path), include_files=False)

    assert [entry["name"] for entry in result["entries"]] == ["a"]


def test_search_matches_file_names_case_insensitively(tmp_path):
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden-readme").write_text("x", encoding="utf-8")
    nested = tmp_path / "docs"
    nested.mkdir()
    (nested / "readme-copy.md").write_text("x", encoding="utf-8")
    node = runtime(tmp_path)

    result = call(node, "node.fs.search", request_id="r1", keyword="readme", path=str(tmp_path))

    assert result["ok"] is True
    assert result["keyword"] == "readme"
    assert result["max_depth"] == 4
    names = [entry["name"] for entry in result["entries"]]
    assert names == ["README.md", "readme-copy.md"]


def test_search_respects_max_depth(tmp_path):
    deep = tmp_path / "one" / "two"
    deep.mkdir(parents=True)
    (deep / "target.md").write_text("x", encoding="utf-8")
    node = runtime(tmp_path)

    shallow = call(
        node, "node.fs.search", request_id="r1", keyword="target", path=str(tmp_path), max_depth=1
    )
    deep_result = call(
        node, "node.fs.search", request_id="r2", keyword="target", path=str(tmp_path), max_depth=4
    )

    assert shallow["entries"] == []
    assert [entry["name"] for entry in deep_result["entries"]] == ["target.md"]


def test_write_replaces_the_file_in_one_chunk(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("old", encoding="utf-8")
    node = runtime(tmp_path)

    result = call(
        node, "node.fs.write", request_id="w1", path=str(target), content="new", mode="overwrite"
    )

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "new"
    assert result["bytes_written"] == 3


def test_write_create_new_refuses_an_existing_file(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("old", encoding="utf-8")
    node = runtime(tmp_path)

    result = call(
        node, "node.fs.write", request_id="w1", path=str(target), content="new", mode="create_new"
    )

    assert result["ok"] is False
    assert result["error_code"] == "invalid_request"
    assert target.read_text(encoding="utf-8") == "old"


def test_chunked_write_keeps_a_temp_file_until_the_last_chunk(tmp_path):
    target = tmp_path / "big.md"
    node = runtime(tmp_path)

    first = call(
        node,
        "node.fs.write",
        request_id="w1",
        path=str(target),
        content="abcd",
        mode="overwrite",
        offset=0,
        total_bytes=8,
    )
    assert first["ok"] is True
    assert not target.exists()

    second = call(
        node,
        "node.fs.write",
        request_id="w2",
        path=str(target),
        content="efgh",
        mode="append",
        offset=4,
        total_bytes=8,
    )
    assert second["ok"] is True
    assert target.read_text(encoding="utf-8") == "abcdefgh"
    assert list(tmp_path.glob("*.castrel-tmp-*")) == []


def test_chunk_continuation_requires_the_matching_offset(tmp_path):
    target = tmp_path / "big.md"
    node = runtime(tmp_path)
    call(
        node,
        "node.fs.write",
        request_id="w1",
        path=str(target),
        content="abcd",
        total_bytes=8,
    )

    result = call(
        node,
        "node.fs.write",
        request_id="w2",
        path=str(target),
        content="efgh",
        offset=2,
        total_bytes=8,
    )

    assert result["ok"] is False
    assert result["error_code"] == "invalid_request"


def test_write_without_create_dirs_fails_on_a_missing_parent(tmp_path):
    target = tmp_path / "missing" / "notes.md"
    node = runtime(tmp_path)

    result = call(
        node,
        "node.fs.write",
        request_id="w1",
        path=str(target),
        content="x",
        create_dirs=False,
    )

    assert result["ok"] is False
    assert result["error_code"] == "not_found"


def test_append_mode_extends_the_existing_file(tmp_path):
    target = tmp_path / "log.txt"
    target.write_text("a", encoding="utf-8")
    node = runtime(tmp_path)

    result = call(
        node, "node.fs.write", request_id="w1", path=str(target), content="b", mode="append"
    )

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "ab"


def test_a_second_concurrent_write_is_rejected(tmp_path):
    target = tmp_path / "notes.md"
    node = runtime(tmp_path)
    node.files._writes.acquire(str(target), "other")

    try:
        result = call(
            node, "node.fs.write", request_id="w1", path=str(target), content="x"
        )
    finally:
        node.files._writes.release(str(target))

    assert result["ok"] is False
    assert result["error_code"] == "invalid_request"


def test_cancel_removes_the_temporary_file(tmp_path):
    target = tmp_path / "big.md"
    node = runtime(tmp_path)
    call(
        node, "node.fs.write", request_id="w1", path=str(target), content="abcd", total_bytes=8
    )
    assert list(tmp_path.glob("*.castrel-tmp-*")) != []

    node.files.cancel("w1")

    assert list(tmp_path.glob("*.castrel-tmp-*")) == []
    assert not target.exists()


def test_edit_replaces_and_reports_the_count(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("x x x", encoding="utf-8")
    node = runtime(tmp_path)

    result = call(
        node,
        "node.fs.edit",
        request_id="e1",
        path=str(target),
        operation="replace",
        old_content="x",
        new_content="y",
    )

    assert result["ok"] is True
    assert result["replaced"] == 3
    assert target.read_text(encoding="utf-8") == "y y y"


def test_edit_append_and_prepend(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("body", encoding="utf-8")
    node = runtime(tmp_path)

    call(node, "node.fs.edit", request_id="e1", path=str(target), operation="append",
         new_content="-end")
    call(node, "node.fs.edit", request_id="e2", path=str(target), operation="prepend",
         new_content="start-")

    assert target.read_text(encoding="utf-8") == "start-body-end"


def test_edit_requires_old_content_for_replace(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("body", encoding="utf-8")
    node = runtime(tmp_path)

    result = call(
        node, "node.fs.edit", request_id="e1", path=str(target), operation="replace",
        new_content="x"
    )

    assert result["ok"] is False
    assert result["error_code"] == "invalid_request"

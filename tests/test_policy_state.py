"""Policy snapshot persisted for `castrel-proxy proxy status`."""

import json

from castrel_proxy.core.policy_state import load_policy_state, save_policy_state


def test_policy_state_round_trip(tmp_path):
    path = tmp_path / "node" / "policy.json"
    save_policy_state(path, version=5, document={"max_concurrency": 8})

    state = load_policy_state(path)

    assert state is not None
    assert state["policy_version"] == 5
    assert state["policy"] == {"max_concurrency": 8}
    assert state["received_at"] > 0
    assert not path.with_name("policy.json.tmp").exists()


def test_policy_state_missing_or_corrupt_reads_as_none(tmp_path):
    path = tmp_path / "policy.json"
    assert load_policy_state(path) is None

    path.write_text("{not json", encoding="utf-8")
    assert load_policy_state(path) is None

    path.write_text(json.dumps({"policy_version": 1}), encoding="utf-8")
    assert load_policy_state(path) is None


def test_policy_state_write_failures_are_swallowed(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    save_policy_state(blocker / "policy.json", version=1, document={})
    save_policy_state(None, version=1, document={})

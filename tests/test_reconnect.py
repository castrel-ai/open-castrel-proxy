"""Outbox bounds and completed-result deduplication."""


from castrel_proxy.transport.spool import Outbox


def test_outbox_is_bounded_and_drops_oldest(tmp_path):
    outbox = Outbox(tmp_path / "outbox.jsonl", max_entries=2)
    outbox.append({"type": "a"})
    outbox.append({"type": "b"})
    outbox.append({"type": "c"})

    assert outbox.gaps == 1
    assert [item["type"] for item in outbox.drain()] == ["b", "c"]
    assert len(outbox) == 0


def test_outbox_survives_restart(tmp_path):
    path = tmp_path / "outbox.jsonl"
    outbox = Outbox(path, max_entries=10)
    outbox.append({"type": "connector.result", "data": {"request_id": "r1"}})

    reopened = Outbox(path, max_entries=10)
    assert [item["type"] for item in reopened.drain()] == ["connector.result"]

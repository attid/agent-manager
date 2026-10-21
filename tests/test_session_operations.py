"""Deferred operations retain history but cannot revert later edits on resume."""

import json

from kuzya.session_operations import (
    acknowledge_items,
    pending_items,
    unapplied_items,
)


def test_only_new_records_are_replayed(tmp_path):
    path = tmp_path / "written_knowledge.json"
    records = [{"name": "draft.md", "content": "first"}]
    path.write_text(json.dumps(records))
    assert list(unapplied_items(path)) == records
    assert list(unapplied_items(path)) == []
    records.append({"name": "draft.md", "content": "approved"})
    path.write_text(json.dumps(records))
    assert list(unapplied_items(path)) == [records[1]]
    assert json.loads(path.read_text()) == records


def test_failed_application_is_not_acknowledged(tmp_path):
    path = tmp_path / "created_agents.json"
    path.write_text('[{"agent_id": "scout"}]')
    pending = unapplied_items(path)
    assert next(pending)["agent_id"] == "scout"
    pending.close()  # application failed before the consumer advanced
    assert list(unapplied_items(path)) == [{"agent_id": "scout"}]
    assert list(unapplied_items(path)) == []


def test_pending_items_are_acknowledged_only_after_success(tmp_path):
    path = tmp_path / "attachments.json"
    first = {"target": "scout", "turn_id": "turn-1"}
    second = {"target": "scout", "turn_id": "turn-2"}
    path.write_text(json.dumps([first, second]))

    pending = pending_items(
        path, match=lambda record: record["turn_id"] == "turn-2"
    )
    assert [record for _, record in pending] == [second]
    # Reading does not acknowledge: the caller has not persisted the effect.
    assert pending_items(
        path, match=lambda record: record["turn_id"] == "turn-2"
    ) == pending

    acknowledge_items(path, pending)
    assert pending_items(path) == [(0, first)]
    assert pending_items(
        path, match=lambda record: record["turn_id"] == "turn-2"
    ) == []

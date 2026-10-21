"""Unit tests for conversation/turn-scoped attachment staging.

These tests use explicit temporary roots only. They exercise the real store
that the persistent conversation handler uses, without touching a host HOME
or any shared production workspace.
"""

from __future__ import annotations

import pathlib

import pytest

from kuzya.attachments import (
    AttachmentSourceMissingError,
    ConversationAttachmentStore,
)


def _source(root: pathlib.Path, name: str, data: bytes) -> pathlib.Path:
    directory = root / "media"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


def _files(directory: pathlib.Path) -> list[str]:
    return sorted(item.name for item in directory.iterdir())


def test_text_only_turn_has_empty_current_context(
    tmp_path: pathlib.Path,
) -> None:
    store = ConversationAttachmentStore(tmp_path / "attachments")
    prepared = store.prepare_turn(
        conversation_id="conv-1",
        turn_id="turn-1",
        input_message_id="msg-1",
        metadata={},
    )

    assert prepared.current == ()
    assert prepared.prior == ()
    assert prepared.inbox_dir.is_dir()
    assert _files(prepared.inbox_dir) == []
    prompt = prepared.prompt()
    assert "Current message attachments: none" in prompt
    assert "do not claim" in prompt
    assert prepared.metadata()["current"] == []


def test_same_basename_across_turns_preserves_distinct_content(
    tmp_path: pathlib.Path,
) -> None:
    store = ConversationAttachmentStore(tmp_path / "attachments")
    first = _source(tmp_path, "photo.jpg", b"first-image-bytes")
    second_dir = tmp_path / "media" / "nested"
    second_dir.mkdir(parents=True)
    second = second_dir / "photo.jpg"
    second.write_bytes(b"second-image-bytes")

    first_turn = store.prepare_turn(
        conversation_id="conv-1",
        turn_id="turn-1",
        input_message_id="msg-1",
        metadata={"media_type": "photo", "media_path": str(first)},
    )
    second_turn = store.prepare_turn(
        conversation_id="conv-1",
        turn_id="turn-2",
        input_message_id="msg-2",
        metadata={"media_type": "photo", "media_path": str(second)},
    )

    assert first_turn.current[0].sha256 != second_turn.current[0].sha256
    assert (first_turn.inbox_dir / first_turn.current[0].inbox_name).read_bytes() == (
        b"first-image-bytes"
    )
    assert (
        second_turn.inbox_dir / second_turn.current[0].inbox_name
    ).read_bytes() == b"second-image-bytes"
    # The earlier turn stays in the same conversation and is clearly prior.
    assert len(second_turn.prior) == 1
    assert second_turn.prior[0].sha256 == first_turn.current[0].sha256
    assert "Earlier attachments from this same conversation" in (
        second_turn.prompt()
    )
    assert "/attachments/turns/" in second_turn.prompt()


def test_retry_same_turn_reuses_staged_file_when_source_disappears(
    tmp_path: pathlib.Path,
) -> None:
    store = ConversationAttachmentStore(tmp_path / "attachments")
    source = _source(tmp_path, "note.txt", b"stable-bytes")
    metadata = {"media_type": "document", "media_path": str(source)}

    first = store.prepare_turn(
        conversation_id="conv-1",
        turn_id="turn-1",
        input_message_id="msg-1",
        metadata=metadata,
    )
    manifest_path = first.current[0].stored_path.parent.parent / "manifest.json"
    manifest_before = manifest_path.read_text(encoding="utf-8")

    source.unlink()
    retried = store.prepare_turn(
        conversation_id="conv-1",
        turn_id="turn-1",
        input_message_id="msg-1",
        metadata=metadata,
    )

    assert retried.current[0].stored_path == first.current[0].stored_path
    assert retried.current[0].sha256 == first.current[0].sha256
    assert (retried.inbox_dir / retried.current[0].inbox_name).read_bytes() == (
        b"stable-bytes"
    )
    assert manifest_path.read_text(encoding="utf-8") == manifest_before
    assert _files(first.current[0].stored_path.parent) == [
        first.current[0].stored_name
    ]


def test_missing_declared_source_fails_closed(tmp_path: pathlib.Path) -> None:
    store = ConversationAttachmentStore(tmp_path / "attachments")
    with pytest.raises(AttachmentSourceMissingError):
        store.prepare_turn(
            conversation_id="conv-1",
            turn_id="turn-1",
            input_message_id="msg-1",
            metadata={
                "media_type": "photo",
                "media_path": str(tmp_path / "absent.jpg"),
            },
        )


def test_unsafe_inbound_names_stay_contained(tmp_path: pathlib.Path) -> None:
    store = ConversationAttachmentStore(tmp_path / "attachments")
    source = tmp_path / "media" / "..\\..\\escape name.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"contained")

    prepared = store.prepare_turn(
        conversation_id="../../etc",
        turn_id="../../passwd",
        input_message_id="../../root",
        metadata={
            "media_type": "photo",
            "media_path": str(source),
            "name": "../../outside/passwd",
        },
    )

    record = prepared.current[0]
    assert "/" not in record.stored_name
    assert "\\" not in record.stored_name
    assert ".." not in record.stored_path.relative_to(tmp_path).parts
    assert record.display_name == "passwd"
    for item in prepared.inbox_dir.iterdir():
        assert item.is_file()
        assert item.parent == prepared.inbox_dir
    assert prepared.store_root == prepared.inbox_dir.parent.parent


def test_conversations_do_not_share_attachment_root(tmp_path: pathlib.Path) -> None:
    store = ConversationAttachmentStore(tmp_path / "attachments")
    first = _source(tmp_path, "one.txt", b"one")
    second = _source(tmp_path, "two.txt", b"two")

    first_turn = store.prepare_turn(
        conversation_id="conv-1",
        turn_id="turn-1",
        input_message_id="msg-1",
        metadata={"media_path": str(first)},
    )
    second_turn = store.prepare_turn(
        conversation_id="conv-2",
        turn_id="turn-1",
        input_message_id="msg-2",
        metadata={"media_path": str(second)},
    )

    assert first_turn.store_root != second_turn.store_root
    assert not first_turn.inbox_dir.is_relative_to(second_turn.store_root)
    assert second_turn.prior == ()
    assert second_turn.current[0].sha256 != first_turn.current[0].sha256

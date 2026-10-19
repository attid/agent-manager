"""Conversation- and turn-scoped staging for inbound attachments.

Incoming Telegram media is first written to the Telegram media directory and
then referenced by message metadata.  This module is the single owner of the
next step: it copies that media into a server-owned, conversation-scoped
store and exposes only the current turn's files as the child Runner inbox.

The layout below an explicit root is::

    <root>/<conversation-key>/turns/<turn-key>/files/<attachment-id><suffix>
    <root>/<conversation-key>/turns/<turn-key>/manifest.json
    <root>/<conversation-key>/views/<turn-key>/<attachment-id>-<display-name>

All directory keys are derived from hashes of server-side identifiers, and
attachment file names never use a raw inbound path or basename.  The display
name is sanitized for the manifest and the disposable inbox view only.

The canonical turn directory is durable across retries.  The ``views``
directory is disposable: every execution recreates the current turn's view so
the child sees exactly that turn's attachments in ``/workspace/inbox``.  The
conversation store itself is mounted read-only at ``/attachments`` so earlier
same-conversation assets remain available but are clearly not current.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
from collections.abc import Mapping
from typing import Any

__all__ = [
    "AttachmentError",
    "AttachmentSourceMissingError",
    "ConversationAttachmentStore",
    "StagedAttachment",
    "TurnAttachments",
]


class AttachmentError(Exception):
    """Base class for attachment staging failures."""


class AttachmentSourceMissingError(AttachmentError):
    """An attachment was declared by metadata but can no longer be read.

    The error is intentionally fail-closed.  Reusing an unrelated file from a
    shared directory would falsely tell the model that it arrived with this
    message.
    """


@dataclasses.dataclass(frozen=True)
class StagedAttachment:
    """One server-owned copy of an inbound attachment."""

    attachment_id: str
    media_type: str
    display_name: str
    stored_name: str
    stored_path: pathlib.Path
    size: int
    sha256: str
    #: Name inside the current-turn disposable inbox view, when applicable.
    inbox_name: str | None = None


@dataclasses.dataclass(frozen=True)
class TurnAttachments:
    """Prepared attachment context for exactly one conversation turn."""

    conversation_id: str
    turn_id: str
    input_message_id: str
    store_root: pathlib.Path
    inbox_dir: pathlib.Path
    current: tuple[StagedAttachment, ...]
    prior: tuple[StagedAttachment, ...]

    def prompt(self) -> str:
        """Return the exact attachment context appended to the turn prompt."""
        lines = [
            "Attachment context (server-generated; not part of the user's text):",
            f"conversation_id: {self.conversation_id}",
            f"turn_id: {self.turn_id}",
            f"input_message_id: {self.input_message_id}",
        ]
        if self.current:
            lines.append(
                "Current message attachments "
                "(read-only, passed with this turn):"
            )
            for item in self.current:
                lines.append(
                    f"- /workspace/inbox/{item.inbox_name} "
                    f"({item.media_type}, {item.size} bytes, "
                    f"sha256={item.sha256})"
                )
        else:
            lines.append(
                "Current message attachments: none. No file arrived with "
                "this message; do not claim that one did."
            )
        if self.prior:
            lines.append(
                "Earlier attachments from this same conversation "
                "(read-only under /attachments, NOT current):"
            )
            for item in self.prior:
                relative = item.stored_path.relative_to(self.store_root)
                lines.append(
                    f"- /attachments/{relative.as_posix()} "
                    f"({item.media_type}, {item.size} bytes, "
                    f"sha256={item.sha256})"
                )
        else:
            lines.append(
                "Earlier attachments from this same conversation: none."
            )
        lines.append(
            "Only files listed as current attachments belong to the current "
            "message.  Never treat project files, another conversation, or "
            "earlier-attachment paths as newly arrived."
        )
        return "\n".join(lines)

    def metadata(self) -> dict[str, Any]:
        """Return the structured manifest stored beside the turn input."""
        return {
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "input_message_id": self.input_message_id,
            "current": [
                self._record_metadata(item, current=True)
                for item in self.current
            ],
            "prior": [
                self._record_metadata(item, current=False)
                for item in self.prior
            ],
        }

    def _record_metadata(
        self, item: StagedAttachment, *, current: bool
    ) -> dict[str, Any]:
        if current:
            sandbox_path = f"/workspace/inbox/{item.inbox_name}"
        else:
            relative = item.stored_path.relative_to(self.store_root)
            sandbox_path = f"/attachments/{relative.as_posix()}"
        return {
            "attachment_id": item.attachment_id,
            "media_type": item.media_type,
            "name": item.display_name,
            "stored_path": str(item.stored_path),
            "sandbox_path": sandbox_path,
            "size": item.size,
            "sha256": item.sha256,
        }


class ConversationAttachmentStore:
    """Stage inbound media under one explicit, conversation-scoped root."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if root is None or str(root) == "":
            raise ValueError("attachment store root must be explicit")
        self.root = pathlib.Path(root).expanduser().resolve()

    def prepare_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        input_message_id: str,
        metadata: Mapping[str, Any],
    ) -> TurnAttachments:
        """Stage current media and build the current-turn inbox view.

        Repeated calls for the same turn reuse the canonical files.  A
        declared source that vanished is only reused when the same slot was
        already staged for this turn; otherwise staging fails rather than
        attaching an unrelated file.
        """
        conversation_dir = self.root / _identity_key(
            "conversation", conversation_id
        )
        turns_dir = conversation_dir / "turns"
        turn_key = _identity_key("turn", turn_id)
        turn_dir = turns_dir / turn_key
        files_dir = turn_dir / "files"
        manifest_path = turn_dir / "manifest.json"
        existing_manifest = _read_manifest(manifest_path)

        entries = _source_entries(metadata)
        current: list[StagedAttachment] = []
        if entries:
            files_dir.mkdir(parents=True, exist_ok=True)
            for slot, entry in enumerate(entries):
                current.append(
                    self._stage_entry(
                        files_dir=files_dir,
                        input_message_id=input_message_id,
                        slot=slot,
                        entry=entry,
                    )
                )
        else:
            # A text-only turn still gets an explicit empty canonical turn and
            # an empty view, so no shared inbox can leak into its context.
            files_dir.mkdir(parents=True, exist_ok=True)

        _remove_unreferenced_files(
            files_dir, {item.stored_name for item in current}
        )
        _write_manifest(
            manifest_path,
            {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "input_message_id": input_message_id,
                "staged_at": (
                    (existing_manifest or {}).get("staged_at")
                    or dt.datetime.now(dt.timezone.utc).isoformat()
                ),
                "attachments": [
                    _stored_record(item) for item in current
                ],
            },
        )

        inbox_dir = self._rebuild_view(
            conversation_dir=conversation_dir,
            turn_key=turn_key,
            current=current,
        )
        current = tuple(
            dataclasses.replace(
                item,
                inbox_name=f"{item.attachment_id}-{item.display_name}",
            )
            for item in current
        )
        prior = self._prior_attachments(turns_dir, exclude_key=turn_key)
        return TurnAttachments(
            conversation_id=conversation_id,
            turn_id=turn_id,
            input_message_id=input_message_id,
            store_root=conversation_dir,
            inbox_dir=inbox_dir,
            current=current,
            prior=prior,
        )

    def _stage_entry(
        self,
        *,
        files_dir: pathlib.Path,
        input_message_id: str,
        slot: int,
        entry: Mapping[str, Any],
    ) -> StagedAttachment:
        raw_path = entry.get("media_path")
        if not isinstance(raw_path, str) or not raw_path:
            raise AttachmentSourceMissingError(
                "attachment metadata has no server-owned media path"
            )
        source = pathlib.Path(raw_path).expanduser()
        if not source.is_absolute():
            raise AttachmentSourceMissingError(
                "attachment media path must be absolute"
            )
        source = source.resolve()
        media_type = str(entry.get("media_type") or "file")
        raw_name = entry.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raw_name = source.name
        display_name = _safe_component(raw_name, fallback="attachment")
        suffix = _safe_suffix(source.suffix)
        attachment_id = _attachment_id(input_message_id, slot)
        stored_name = f"{attachment_id}{suffix}"
        stored_path = files_dir / stored_name

        if source.is_file():
            digest, size = _copy_atomic(source, stored_path)
        elif stored_path.is_file():
            digest, size = _file_digest(stored_path)
        else:
            raise AttachmentSourceMissingError(
                "declared attachment source is missing and was not staged "
                f"for this turn: {source}"
            )
        return StagedAttachment(
            attachment_id=attachment_id,
            media_type=media_type,
            display_name=display_name,
            stored_name=stored_name,
            stored_path=stored_path,
            size=size,
            sha256=digest,
        )

    def _rebuild_view(
        self,
        *,
        conversation_dir: pathlib.Path,
        turn_key: str,
        current: list[StagedAttachment],
    ) -> pathlib.Path:
        views_dir = conversation_dir / "views"
        views_dir.mkdir(parents=True, exist_ok=True)
        inbox_dir = views_dir / turn_key
        if inbox_dir.is_symlink() or inbox_dir.is_file():
            inbox_dir.unlink()
        elif inbox_dir.is_dir():
            shutil.rmtree(inbox_dir)
        inbox_dir.mkdir(parents=False, exist_ok=False)
        for item in current:
            inbox_name = f"{item.attachment_id}-{item.display_name}"
            shutil.copyfile(item.stored_path, inbox_dir / inbox_name)
        return inbox_dir

    def _prior_attachments(
        self, turns_dir: pathlib.Path, *, exclude_key: str
    ) -> tuple[StagedAttachment, ...]:
        prior: list[StagedAttachment] = []
        if not turns_dir.is_dir():
            return ()
        for turn_dir in sorted(turns_dir.iterdir()):
            if turn_dir.name == exclude_key or not turn_dir.is_dir():
                continue
            manifest = _read_manifest(turn_dir / "manifest.json")
            if manifest is None:
                continue
            for record in manifest.get("attachments") or []:
                if not isinstance(record, Mapping):
                    continue
                stored_name = record.get("stored_name")
                if not isinstance(stored_name, str) or not stored_name:
                    continue
                stored_path = turn_dir / "files" / stored_name
                if not stored_path.is_file():
                    continue
                prior.append(
                    StagedAttachment(
                        attachment_id=str(record.get("attachment_id") or ""),
                        media_type=str(record.get("media_type") or "file"),
                        display_name=str(
                            record.get("display_name") or "attachment"
                        ),
                        stored_name=stored_name,
                        stored_path=stored_path,
                        size=int(record.get("size") or 0),
                        sha256=str(record.get("sha256") or ""),
                    )
                )
        return tuple(prior)


def _source_entries(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return declared attachment sources in deterministic slot order."""
    entries: list[dict[str, Any]] = []
    raw_items = metadata.get("attachments")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            media_path = item.get("media_path") or item.get("path")
            if isinstance(media_path, str) and media_path:
                entries.append(
                    {
                        "media_path": media_path,
                        "media_type": item.get("media_type") or "file",
                        "name": item.get("name") or item.get("filename"),
                    }
                )
    if not entries:
        media_path = metadata.get("media_path")
        if isinstance(media_path, str) and media_path:
            entries.append(
                {
                    "media_path": media_path,
                    "media_type": metadata.get("media_type") or "file",
                    "name": metadata.get("media_name")
                    or metadata.get("name")
                    or metadata.get("filename"),
                }
            )
    return entries


def _identity_key(kind: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{kind} id must be a non-empty string")
    return hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:32]


def _attachment_id(input_message_id: str, slot: int) -> str:
    key = f"{input_message_id}:{slot}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:24]


def _safe_component(value: str, *, fallback: str) -> str:
    text = str(value or "").replace("\\", "/")
    name = text.rsplit("/", 1)[-1]
    cleaned = "".join(
        character
        if character.isprintable() and character not in "/\\\x00"
        else "_"
        for character in name
    ).strip()
    if cleaned in {"", ".", ".."}:
        return fallback
    return cleaned


def _safe_suffix(value: str) -> str:
    suffix = _safe_component(value, fallback="")
    if suffix in {"", ".", ".."}:
        return ""
    if not suffix.startswith("."):
        return f".{suffix}"
    return suffix


def _copy_atomic(source: pathlib.Path, destination: pathlib.Path) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                writer.write(chunk)
                size += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return digest.hexdigest(), size


def _file_digest(path: pathlib.Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as reader:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _remove_unreferenced_files(
    files_dir: pathlib.Path, keep_names: set[str]
) -> None:
    for item in files_dir.iterdir():
        if item.is_file() and item.name not in keep_names:
            item.unlink(missing_ok=True)
        elif item.name.endswith(".tmp"):
            item.unlink(missing_ok=True)


def _stored_record(item: StagedAttachment) -> dict[str, Any]:
    return {
        "attachment_id": item.attachment_id,
        "media_type": item.media_type,
        "display_name": item.display_name,
        "stored_name": item.stored_name,
        "size": item.size,
        "sha256": item.sha256,
    }


def _write_manifest(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(data + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_manifest(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None

"""Freeze one agent-profile revision into an executable Pi reply handler.

This is a narrow host-side factory. It reads one immutable
:class:`~agent_manager.storage.AgentProfile` snapshot, exports it to a new
directory and constructs the existing
:class:`~agent_manager.pi_reply.PiReplyHandler` against that export. It does
not add an API, worker loop, history loader, model call or Runner change.

One frozen profile, one execution
---------------------------------

The factory is intended to be called immediately before one execution::

    frozen = freeze_profile_handler(
        database,
        "triage",
        runner_argv_prefix=["/path/to/host/python", "-m", "pi_runner"],
        artifact_root="/var/lib/agent-manager/profiles",
    )
    processor.run_once(frozen)

The snapshot is loaded and written once. Later profile revisions in the
database cannot change an already-built handler, because the handler only
reads its exported ``runner_role.json`` and never queries the registry again.
A caller that wants the new revision builds a new handler.

Export layout
-------------

Every call creates a new directory named with a random UUID below
``artifact_root``. The directory name is independent of ``agent_id`` and user
input, so exports never overwrite one another::

    <artifact_root>/<uuid>/
        snapshot.json       full profile snapshot and declarative bindings
        system_prompt.md    exact stored system prompt
        runner_role.json     generated Runner role configuration
        runs/               PiReplyHandler per-execution artifacts

The role configuration starts from the stored ``runner_config`` object, gets
the canonical ``agent_id`` from the profile, and appends the exported
``system_prompt.md`` path to the existing ``instructions`` list. Existing
instruction entries and all other trusted configuration are preserved. The
stored JSON in the registry is never modified.

Skills and knowledge
--------------------

``skills`` and ``knowledge`` are preserved in ``snapshot.json`` for
traceability only. This factory does not load them, grant write access or
claim that the model used them. :class:`FrozenProfileHandler` reports
``skills_connected`` and ``knowledge_connected`` as false.

Credentials and tools remain the Runner's concern. The factory neither reads
nor copies credentials, and provider references already present in the
trusted ``runner_config`` stay opaque.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import pathlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .pi_reply import PiReplyHandler, PiRunProvenance
from .runtime_defaults import ResolvedRuntimeDefaults, RuntimeDefaultsResolver
from .secretary import TurnInput
from .storage import (
    AgentProfile,
    AgentProfileNotFoundError,
    AgentProfileRepository,
    Database,
)

__all__ = [
    "FrozenProfileHandler",
    "ProfileRuntimeConfigurationError",
    "ProfileRuntimeError",
    "ProfileSnapshot",
    "ProfileSnapshotNotFoundError",
    "build_profile_handler",
    "freeze_profile_handler",
]


class ProfileRuntimeError(Exception):
    """Base class for failures while freezing a profile for execution."""


class ProfileSnapshotNotFoundError(
    ProfileRuntimeError, AgentProfileNotFoundError
):
    """No stored snapshot matches the requested ``agent_id`` and revision.

    The error is also an :class:`AgentProfileNotFoundError` so callers already
    handling registry misses keep working.
    """

    def __init__(self, agent_id: str, revision: int | None = None) -> None:
        if revision is None:
            message = f"agent profile not found: {agent_id}"
        else:
            message = (
                f"agent profile snapshot not found: {agent_id!r} "
                f"revision {revision}"
            )
        ProfileRuntimeError.__init__(self, message)
        self.agent_id = agent_id
        self.revision = revision


class ProfileRuntimeConfigurationError(ProfileRuntimeError):
    """Stored trusted configuration cannot be represented as a role JSON."""


@dataclasses.dataclass(frozen=True)
class ProfileSnapshot:
    """In-memory copy of the one registry revision bound to a handler."""

    agent_id: str
    revision: int
    name: str
    role: str
    system_prompt: str
    runner_config: Mapping[str, Any]
    skills: Sequence[Any]
    knowledge: Mapping[str, Any]
    created_at: str | None

    @property
    def version(self) -> int:
        """Alias for :attr:`revision` used by host integrations."""
        return self.revision


@dataclasses.dataclass(frozen=True)
class FrozenProfileHandler:
    """Inspectable result of :func:`freeze_profile_handler`.

    The wrapper is itself callable with the usual ``Handler`` contract
    (``TurnInput -> str``), delegates to :attr:`handler`, and exposes its
    ``last_execution`` provenance. Callers may pass either the wrapper or
    ``wrapper.handler`` to :class:`~agent_manager.secretary.TurnProcessor`.
    """

    handler: PiReplyHandler
    profile: ProfileSnapshot
    export_dir: pathlib.Path
    snapshot_path: pathlib.Path
    prompt_path: pathlib.Path
    role_config_path: pathlib.Path
    runs_dir: pathlib.Path

    def __call__(self, turn_input: TurnInput) -> str:
        """Run the frozen profile through the existing Pi reply handler."""
        return self.handler(turn_input)

    @property
    def last_execution(self) -> PiRunProvenance | None:
        """Result record of the most recent delegated handler call."""
        return self.handler.last_execution

    @property
    def agent_id(self) -> str:
        return self.profile.agent_id

    @property
    def revision(self) -> int:
        return self.profile.revision

    @property
    def version(self) -> int:
        """Alias for :attr:`revision`."""
        return self.profile.revision

    @property
    def config_path(self) -> pathlib.Path:
        """Alias for :attr:`role_config_path`."""
        return self.role_config_path

    @property
    def artifact_dir(self) -> pathlib.Path:
        """Directory holding per-execution run artifacts."""
        return self.runs_dir

    @property
    def skills_connected(self) -> bool:
        """Skills are declarative exports only; nothing was loaded."""
        return False

    @property
    def knowledge_connected(self) -> bool:
        """Knowledge is a declarative export only; nothing was loaded."""
        return False

    @property
    def not_connected(self) -> tuple[str, ...]:
        """Bindings deliberately left disconnected by this task."""
        return ("skills", "knowledge")


def freeze_profile_handler(
    repository: AgentProfileRepository | Database | None = None,
    agent_id: str | None = None,
    *,
    database: Database | None = None,
    revision: int | None = None,
    runner_argv_prefix: Sequence[str],
    artifact_root: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    runtime_defaults: RuntimeDefaultsResolver | None = None,
) -> FrozenProfileHandler:
    """Load one profile snapshot once and freeze it into a handler.

    Args:
        repository: An :class:`AgentProfileRepository` or the
            :class:`Database` used to build one.
        agent_id: Registry identity whose snapshot is frozen.
        database: Alternative keyword spelling for passing the database.
        revision: Exact revision to freeze. ``None`` (the default) selects the
            latest revision at factory-call time.
        runner_argv_prefix: Trusted Runner argument vector, passed unchanged
            to :class:`PiReplyHandler`.
        artifact_root: Parent directory for unique export directories.
        env: Optional child environment; ``None`` inherits the caller's
            process environment unchanged.
        runtime_defaults: Optional standalone
            :class:`~agent_manager.runtime_defaults.RuntimeDefaultsResolver`.
            When supplied, it is resolved once for this frozen execution and
            the resolved provider/model/auth/models-file values are merged
            into the exported role configuration. Selection evidence is saved
            in the export and adapter provenance. ``None`` (the default)
            preserves the previous behaviour exactly and reads no host
            settings.

    Returns:
        A callable :class:`FrozenProfileHandler` with the handler, immutable
        profile copy, export paths and revision.

    Raises:
        ProfileSnapshotNotFoundError: when the profile or requested revision
            does not exist.
        ProfileRuntimeConfigurationError: when stored ``runner_config`` is not
            a JSON object or ``instructions`` has an unsupported shape.
        ValueError: propagated from :class:`PiReplyHandler` for an invalid
            trusted argv prefix or relative role path.
    """
    repository, agent_id = _resolve_source(repository, agent_id, database)

    if revision is None:
        profile = repository.get_current(agent_id)
    else:
        profile = repository.get_revision(agent_id, revision)
    if profile is None:
        raise ProfileSnapshotNotFoundError(agent_id, revision)

    # Copy everything before any file is written. The returned wrapper and
    # the generated role JSON therefore cannot observe later registry edits.
    snapshot = _profile_snapshot(profile)

    # Resolve runtime defaults once for this frozen execution. The stored
    # profile snapshot keeps the original trusted runner_config; only the
    # exported role configuration receives the resolved values.
    resolved_defaults: ResolvedRuntimeDefaults | None = None
    resolved_role_config: Mapping[str, Any] = snapshot.runner_config
    if runtime_defaults is not None:
        resolved_defaults = runtime_defaults.resolve(snapshot.runner_config)
        resolved_role_config = resolved_defaults.role_config

    export_dir = (
        pathlib.Path(artifact_root).absolute()
        / uuid.uuid4().hex
    )
    runs_dir = export_dir / "runs"

    prompt_path = export_dir / "system_prompt.md"
    role_config_path = export_dir / "runner_role.json"
    snapshot_path = export_dir / "snapshot.json"

    # Validate and build the complete role document before creating the export
    # directory, so bad trusted configuration leaves no partial export behind.
    role_config = _build_role_config(
        resolved_role_config,
        agent_id=snapshot.agent_id,
        prompt_path=prompt_path,
    )

    export_dir.mkdir(parents=True, exist_ok=False)
    _write_text(prompt_path, snapshot.system_prompt)
    _write_json(role_config_path, role_config)
    _write_json(
        snapshot_path,
        _snapshot_document(
            snapshot,
            export_dir=export_dir,
            prompt_path=prompt_path,
            role_config_path=role_config_path,
            runs_dir=runs_dir,
            runtime_defaults=resolved_defaults,
        ),
    )

    handler = PiReplyHandler(
        runner_argv_prefix=runner_argv_prefix,
        role_config_path=role_config_path,
        artifact_dir=runs_dir,
        env=env,
        profile_agent_id=snapshot.agent_id,
        profile_revision=snapshot.revision,
        profile_snapshot_path=snapshot_path,
        runtime_defaults_provenance=(
            resolved_defaults.to_provenance()
            if resolved_defaults is not None
            else None
        ),
    )
    return FrozenProfileHandler(
        handler=handler,
        profile=snapshot,
        export_dir=export_dir,
        snapshot_path=snapshot_path,
        prompt_path=prompt_path,
        role_config_path=role_config_path,
        runs_dir=runs_dir,
    )


#: Short alias for host code that builds one handler per execution.
build_profile_handler = freeze_profile_handler


def _resolve_source(
    repository: AgentProfileRepository | Database | None,
    agent_id: str | None,
    database: Database | None,
) -> tuple[AgentProfileRepository, str]:
    """Normalise ``repository``/``database`` arguments and validate identity."""
    if repository is None:
        if database is None:
            raise TypeError("repository or database is required")
        repository = database
    elif database is not None:
        raise TypeError("pass either repository or database, not both")

    if isinstance(repository, Database):
        profiles = AgentProfileRepository(repository)
    elif isinstance(repository, AgentProfileRepository):
        profiles = repository
    else:
        raise TypeError("repository must be AgentProfileRepository or Database")

    if not isinstance(agent_id, str):
        raise TypeError("agent_id must be a string")
    return profiles, agent_id


def _profile_snapshot(profile: AgentProfile) -> ProfileSnapshot:
    """Copy the detached ORM row into immutable plain Python values."""
    created_at = profile.created_at
    return ProfileSnapshot(
        agent_id=profile.agent_id,
        revision=profile.revision,
        name=profile.name,
        role=profile.role,
        system_prompt=profile.system_prompt,
        runner_config=copy.deepcopy(profile.runner_config),
        skills=copy.deepcopy(profile.skills),
        knowledge=copy.deepcopy(profile.knowledge),
        created_at=created_at.isoformat() if created_at is not None else None,
    )


def _build_role_config(
    runner_config: Mapping[str, Any],
    *,
    agent_id: str,
    prompt_path: pathlib.Path,
) -> dict[str, Any]:
    """Return the generated role JSON without mutating stored configuration."""
    if not isinstance(runner_config, Mapping):
        raise ProfileRuntimeConfigurationError(
            "stored runner_config must be a JSON object"
        )
    role_config = copy.deepcopy(dict(runner_config))

    # The registry profile is the canonical identity; a stale or malformed
    # stored value cannot override it.
    role_config["agent_id"] = agent_id

    existing = role_config.get("instructions")
    if existing is None:
        instructions: list[Any] = []
    elif isinstance(existing, str):
        # Accept a single trusted instruction entry without changing it.
        instructions = [existing]
    elif isinstance(existing, (list, tuple)):
        instructions = list(existing)
    else:
        raise ProfileRuntimeConfigurationError(
            "stored runner_config instructions must be a string or array"
        )
    instructions.append(str(prompt_path))
    role_config["instructions"] = instructions

    if "pi_command" not in role_config or "runtime_paths" not in role_config:
        try:
            from .knowledge import get_default_pi_runtime
            default_cmd, default_runtime = get_default_pi_runtime()
            if "pi_command" not in role_config:
                role_config["pi_command"] = default_cmd
            if "runtime_paths" not in role_config:
                role_config["runtime_paths"] = default_runtime
        except Exception:
            pass

    return role_config


def _snapshot_document(
    snapshot: ProfileSnapshot,
    *,
    export_dir: pathlib.Path,
    prompt_path: pathlib.Path,
    role_config_path: pathlib.Path,
    runs_dir: pathlib.Path,
    runtime_defaults: ResolvedRuntimeDefaults | None = None,
) -> dict[str, Any]:
    """Build the inspectable export manifest for one frozen revision."""
    skills = copy.deepcopy(list(snapshot.skills))
    knowledge = copy.deepcopy(dict(snapshot.knowledge))
    document = {
        "agent_id": snapshot.agent_id,
        "revision": snapshot.revision,
        "name": snapshot.name,
        "role": snapshot.role,
        "system_prompt": snapshot.system_prompt,
        "runner_config": copy.deepcopy(dict(snapshot.runner_config)),
        "skills": skills,
        "knowledge": knowledge,
        "created_at": snapshot.created_at,
        "bindings": {
            "skills": skills,
            "knowledge": knowledge,
            "connected": False,
            "not_connected": ["skills", "knowledge"],
        },
        "runtime": {
            "export_dir": str(export_dir),
            "system_prompt_path": str(prompt_path),
            "role_config_path": str(role_config_path),
            "runs_dir": str(runs_dir),
        },
    }
    if runtime_defaults is not None:
        document["runtime_defaults"] = runtime_defaults.to_provenance()
    return document


def _write_text(path: pathlib.Path, text: str) -> None:
    """Write one export file durably as UTF-8."""
    data = text.encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: pathlib.Path, value: Any) -> None:
    _write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

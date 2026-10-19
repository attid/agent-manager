"""Immutable versioned agent-profile storage boundary.

An *agent profile* is a named, versioned snapshot of an agent's identity and
runtime configuration. This module stores snapshots; it does not run agents,
load skills, call a model, talk to Telegram or expose HTTP.

Storage model
-------------

One table, ``agent_profiles``, keyed by ``(agent_id, revision)``. Every row is
a complete immutable snapshot. The *current* profile of an agent is the row
with the maximum ``revision`` for that ``agent_id``. Rows are never updated in
place: :meth:`AgentProfileRepository.update` appends a new revision and leaves
all older snapshots untouched.

``role`` and ``agent_id`` are free-form strings. ``runner_config`` and
``knowledge`` are JSON objects and ``skills`` is a JSON array of binding
objects. Their contents are opaque, trusted configuration data at this
boundary; execution validate them later. No length caps, skill-count limits,
role allowlists or hardcoded agents exist here.

Concurrency
-----------

An update is a single ``INSERT ... SELECT`` guarded by ``expected_revision``
and by the row being the current maximum revision. The database therefore
enforces the revision check atomically, and the composite primary key rejects
any duplicate ``(agent_id, revision)`` even under a race. Two writers that
present the same ``expected_revision`` cannot both commit: at most one inserts
the next revision, the other raises
:class:`AgentProfileRevisionConflictError`. No update is lost and no revision
is duplicated.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from .database import Database
from .models import AgentProfile

CORE_SYSTEM_AGENTS: frozenset[str] = frozenset(
    {"system-secretary", "system-code-reviewer", "system-log-analyzer"}
)

#: Sentinel distinguishing "field omitted" from "field supplied as ``None``".
#: Agent-profile scalar fields are non-nullable, so ``None`` is not a valid
#: replacement value, but the sentinel keeps the partial-update contract
#: explicit and independent of that fact.
_UNSET: Any = object()


class AgentProfileNotFoundError(Exception):
    """Raised when an operation targets an agent with no stored snapshot."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent profile not found: {agent_id}")
        self.agent_id = agent_id


class DuplicateAgentProfileError(Exception):
    """Raised when creating an agent whose ``agent_id`` already has snapshots."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent profile already exists: {agent_id}")
        self.agent_id = agent_id


class AgentProfileRevisionConflictError(Exception):
    """Raised when an update does not target the current revision.

    ``actual_revision`` is the current revision observed in the database, or
    ``None`` when the agent disappeared between the failed insert and the
    diagnostic read.
    """

    def __init__(
        self,
        agent_id: str,
        expected_revision: int,
        actual_revision: int | None,
    ) -> None:
        super().__init__(
            f"agent profile revision conflict for {agent_id!r}: "
            f"expected revision {expected_revision}, "
            f"current revision {actual_revision}"
        )
        self.agent_id = agent_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


def _is_agent_profile_pk_conflict(error: IntegrityError) -> bool:
    """Return whether ``error`` is a duplicate ``(agent_id, revision)``.

    Only the composite primary-key conflict is translated into a typed
    error; unrelated integrity failures propagate unchanged.
    """
    original = error.orig
    message = str(original)
    if (
        "agent_profiles" not in message
        or "UNIQUE constraint failed" not in message
    ):
        return False
    code = getattr(original, "sqlite_errorcode", None)
    if code is not None and code not in (
        sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
        sqlite3.SQLITE_CONSTRAINT_UNIQUE,
    ):
        return False
    return True


class AgentProfileRepository:
    """Create and version immutable agent-profile snapshots.

    Args:
        database: The :class:`~agent_manager.storage.database.Database` that
            owns the SQLite file. Each repository call opens a short
            transaction on its own connection.

    Read behaviour:
        * :meth:`get_current` and :meth:`get_revision` return ``None`` when no
          matching snapshot exists.
        * :meth:`list_history` returns an empty list for an unknown agent.
        * Writes raise typed exceptions: :class:`DuplicateAgentProfileError`
          on creation, :class:`AgentProfileNotFoundError` when updating an
          unknown agent and :class:`AgentProfileRevisionConflictError` when
          ``expected_revision`` is stale.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # -- writes ----------------------------------------------------------

    def create(
        self,
        *,
        agent_id: str,
        name: str,
        role: str,
        system_prompt: str,
        runner_config: Mapping[str, Any] | None = None,
        skills: Sequence[Mapping[str, Any]] | None = None,
        knowledge: Mapping[str, Any] | None = None,
    ) -> AgentProfile:
        """Create revision ``1`` of ``agent_id`` and return the snapshot.

        Args:
            agent_id: Free-form agent identifier.
            name: Human-readable profile name.
            role: Free-form role string; never checked against a list.
            system_prompt: Explicit identity/system prompt text.
            runner_config: Opaque JSON object, default empty.
            skills: Opaque JSON array of binding objects, default empty.
            knowledge: Opaque JSON object, default empty.

        Raises:
            DuplicateAgentProfileError: when ``agent_id`` already has any
                snapshot.
        """
        profile = AgentProfile(
            agent_id=agent_id,
            revision=1,
            name=name,
            role=role,
            system_prompt=system_prompt,
            runner_config=(
                dict(runner_config) if runner_config is not None else {}
            ),
            skills=[dict(binding) for binding in skills]
            if skills is not None
            else [],
            knowledge=dict(knowledge) if knowledge is not None else {},
        )
        with self._database.session() as session:
            session.add(profile)
            try:
                session.flush()
            except IntegrityError as error:
                if _is_agent_profile_pk_conflict(error):
                    raise DuplicateAgentProfileError(agent_id) from error
                raise
        return profile

    def update(
        self,
        *,
        agent_id: str,
        expected_revision: int,
        name: Any = _UNSET,
        role: Any = _UNSET,
        system_prompt: Any = _UNSET,
        runner_config: Any = _UNSET,
        skills: Any = _UNSET,
        knowledge: Any = _UNSET,
    ) -> AgentProfile:
        """Append ``expected_revision + 1`` with supplied fields replaced.

        Omitted keyword arguments keep their value from the current snapshot,
        so the new revision is always a complete snapshot. Supplied fields
        are stored exactly as given. Unknown keyword names are rejected by
        Python.

        Args:
            agent_id: Agent whose current revision is being replaced.
            expected_revision: The revision the caller believes is current.
            name: Replacement name when supplied.
            role: Replacement free-form role when supplied.
            system_prompt: Replacement prompt text when supplied.
            runner_config: Replacement JSON object when supplied.
            skills: Replacement JSON binding array when supplied.
            knowledge: Replacement JSON object when supplied.

        Raises:
            AgentProfileNotFoundError: when ``agent_id`` has no snapshot.
            AgentProfileRevisionConflictError: when ``expected_revision`` is
                not the current revision; the current revision is reported
                but the stored snapshots are left unchanged.
        """
        new_revision = expected_revision + 1
        replacements = {
            "name": name,
            "role": role,
            "system_prompt": system_prompt,
            "runner_config": runner_config,
            "skills": skills,
            "knowledge": knowledge,
        }
        source: list[Any] = [AgentProfile.agent_id, literal(new_revision)]
        for field_name, value in replacements.items():
            column = getattr(AgentProfile, field_name)
            if value is _UNSET:
                source.append(column)
            else:
                source.append(literal(value, type_=column.type))

        current_revision = (
            select(func.max(AgentProfile.revision))
            .where(AgentProfile.agent_id == agent_id)
            .scalar_subquery()
        )
        select_statement = select(*source).where(
            AgentProfile.agent_id == agent_id,
            AgentProfile.revision == expected_revision,
            AgentProfile.revision == current_revision,
        )
        statement = insert(AgentProfile).from_select(
            [
                "agent_id",
                "revision",
                "name",
                "role",
                "system_prompt",
                "runner_config",
                "skills",
                "knowledge",
            ],
            select_statement,
        )

        with self._database.session() as session:
            try:
                result = session.execute(statement)
            except IntegrityError as error:
                if _is_agent_profile_pk_conflict(error):
                    raise AgentProfileRevisionConflictError(
                        agent_id,
                        expected_revision,
                        self._current_revision(agent_id),
                    ) from error
                raise

            if result.rowcount != 1:
                actual = self._current_revision(agent_id)
                if actual is None:
                    raise AgentProfileNotFoundError(agent_id)
                raise AgentProfileRevisionConflictError(
                    agent_id, expected_revision, actual
                )

            profile = session.get(AgentProfile, (agent_id, new_revision))
            assert profile is not None
        return profile

    # -- reads -----------------------------------------------------------

    def get_current(self, agent_id: str) -> AgentProfile | None:
        """Return the highest-revision snapshot of ``agent_id`` or ``None``."""
        statement = (
            select(AgentProfile)
            .where(AgentProfile.agent_id == agent_id)
            .order_by(AgentProfile.revision.desc())
            .limit(1)
        )
        with self._database.session() as session:
            return session.scalars(statement).one_or_none()

    def get_revision(
        self, agent_id: str, revision: int
    ) -> AgentProfile | None:
        """Return the snapshot at exactly ``revision`` or ``None``."""
        with self._database.session() as session:
            return session.get(AgentProfile, (agent_id, revision))

    def archive(
        self, agent_id: str, expected_revision: int | None = None
    ) -> AgentProfile:
        """Mark an agent profile as archived by creating a new revision."""
        if agent_id in CORE_SYSTEM_AGENTS:
            raise ValueError(f"Core system agent '{agent_id}' cannot be archived")
        current = self.get_current(agent_id)
        if current is None:
            raise AgentProfileNotFoundError(agent_id)
        exp_rev = expected_revision if expected_revision is not None else current.revision
        cfg = dict(current.runner_config or {})
        cfg["archived"] = True
        return self.update(
            agent_id=agent_id,
            expected_revision=exp_rev,
            runner_config=cfg,
        )

    def list_current(self, include_archived: bool = False) -> list[AgentProfile]:
        """Return the current snapshot of every agent, ordered by agent id."""
        outer = aliased(AgentProfile)
        current_revision = (
            select(func.max(AgentProfile.revision))
            .where(AgentProfile.agent_id == outer.agent_id)
            .correlate(outer)
            .scalar_subquery()
        )
        statement = (
            select(outer)
            .where(outer.revision == current_revision)
            .order_by(outer.agent_id)
        )
        with self._database.session() as session:
            profiles = list(session.scalars(statement))
            if not include_archived:
                profiles = [
                    p
                    for p in profiles
                    if not (
                        isinstance(p.runner_config, dict)
                        and p.runner_config.get("archived") is True
                    )
                    and p.role != "archived"
                ]
            return profiles

    def list_history(self, agent_id: str) -> list[AgentProfile]:
        """Return every snapshot of ``agent_id`` ordered by revision.

        An unknown agent yields an empty list.
        """
        statement = (
            select(AgentProfile)
            .where(AgentProfile.agent_id == agent_id)
            .order_by(AgentProfile.revision)
        )
        with self._database.session() as session:
            return list(session.scalars(statement))

    # -- helpers ---------------------------------------------------------

    def _current_revision(self, agent_id: str) -> int | None:
        """Read the current revision of ``agent_id`` on a fresh connection.

        Used for diagnostics after a conditional insert matched no row or
        raised an integrity error; the caller's session may be unusable at
        that point, so this always uses a separate short transaction.
        """
        with self._database.session() as session:
            return session.scalar(
                select(func.max(AgentProfile.revision)).where(
                    AgentProfile.agent_id == agent_id
                )
            )

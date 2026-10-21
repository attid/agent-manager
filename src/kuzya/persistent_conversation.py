"""Persistent conversation Pi handler using the runtime binding repository.

This module is the small host-side join between four already-accepted
boundaries:

* :class:`~kuzya.storage.ConversationRuntimeRepository` (AM-018),
  which persists the per-conversation profile pin, exported paths, native
  Runner session id and the single model-call reservation;
* :func:`kuzya.profile_runtime.freeze_profile_handler`, which builds
  the immutable frozen profile export for the pinned revision; and
* :class:`kuzya.pi_reply.PiReplyHandler` (AM-017), the existing
  ``run``/``resume`` child-process adapter; and
* :class:`~kuzya.runtime_defaults.RuntimeDefaultsResolver` (AM-020),
  which resolves the per-call provider/model/auth selection outside the
  durable registry.

It adds no dispatcher, HTTP endpoint, Telegram integration, tool API, Runner
change, history loader or compaction. It does not import the sibling Runner.
The handler is a plain ``(TurnInput) -> str`` callable, so it drops straight
into the existing :class:`~kuzya.secretary.TurnProcessor`.

Lifecycle
---------

Constructing a :class:`PersistentConversationHandler` launches no child and
writes no files. Every call:

1. calls :meth:`ConversationRuntimeRepository.reserve_call` with the real
   conversation, turn id and attempt count. On a brand-new conversation the
   repository pins the current profile revision itself; this module never
   resolves "latest" separately, so it cannot race that choice;
2. when the repository returns a cached result (the turn was already stored
   as the binding's last successful turn, for example after a crash before
   ``TurnProcessor.complete``), returns that stored reply text without
   invoking any child;
3. otherwise owns the reservation and prepares the pinned context. A first
   call freezes the pinned revision and persists the export paths through
   :meth:`ConversationRuntimeRepository.attach_prepared`; a prepared binding
   with no stored result reuses those paths for the first run; a later call
   reconstructs the pinned paths and native session (``resume``);
4. resolves the injected runtime defaults once for this call against the
   pinned base role policy and writes a unique execution-role document beside
   the base export. The base role, snapshot, prompt, state directory, profile
   revision, native session and earlier execution artifacts are never
   modified;
5. launches the child outside every database transaction and, on success,
   requires a nonempty returned Runner ``session_id``; and
6. stores the reply, session id and per-call provenance (including the
   actual execution-role path and the selection evidence) and releases the
   reservation through :meth:`ConversationRuntimeRepository.finish_call`
   before returning the text to the processor.

A successful fresh call details: the pinned profile revision comes from the
binding, the frozen export is created for exactly that revision, and the
trusted ``state_dir`` from the generated role configuration is normalized
and persisted so a later process can resume from the database alone without
any in-memory session map. A cached successful result returns before any new
runtime-default resolution and never launches again.

Failure policy
--------------

After a reservation is owned, a failure is classified by the proven launch
boundary. A failure while reading the pinned base policy, resolving the
runtime defaults or generating and handing over the execution role is proven
to occur before the child adapter is invoked: the reservation is released
with :meth:`ConversationRuntimeRepository.release_prelaunch_call` (never the
blocked latch), so an operator can repair the defaults file and explicitly
retry the failed turn without a duplicate model call. Everything else,
including export, launch, protocol, runner and missing-session failures, is
fail-closed: the reservation is released into the repository's blocked latch
with a diagnostic reason and the original typed error is re-raised. There is
no automatic replay, fresh-session fallback or general unlock. A missing
returned session id is a failure and can never become an unbound "successful"
call. The existing ``TurnProcessor`` failure, retry and recovery rules are
unchanged; a failed turn still records no assistant reply.

Reconstruction
--------------

All resume state lives in the database and on disk. A brand-new handler
object, in a brand-new process, reads ``agent_id``, ``profile_revision``,
the pinned export paths and ``runner_session_id`` from the binding,
re-resolves the current runtime defaults and rebuilds the adapter against a
fresh execution role. Nothing about the conversation is kept in handler
memory.

``last_execution``
------------------

:attr:`PersistentConversationHandler.last_execution` mirrors the existing
adapters. After a fresh child launch it is the adapter's
:class:`~kuzya.pi_reply.PiRunProvenance`; after a cached recovery it
is the provenance ``Mapping`` previously stored on the binding (there was no
new child, so there is no new ``PiRunProvenance``). It is ``None`` before the
first call.

Host invocation sketch
----------------------

.. code-block:: python

    from kuzya.persistent_conversation import (
        PersistentConversationHandler,
    )
    from kuzya.secretary import TurnProcessor
    from kuzya.storage import Database

    database = Database.from_path("/var/lib/kuzya/kuzya.db")
    handler = PersistentConversationHandler(
        database,
        "triage",
        runner_argv_prefix=["/path/to/host/python", "-m", "pi_runner"],
        artifact_root="/var/lib/kuzya/conversations",
        # env defaults to None: the child inherits os.environ unchanged.
    )

    result = TurnProcessor(database).run_once(handler)
    if result.status == "failed":
        print(handler.last_execution)

This implements native conversation continuation only. Context budget,
rotation, summaries and reconciliation of an uncertain call are still not
implemented; the handler is not release-ready for unbounded conversations.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .attachments import ConversationAttachmentStore
from .capabilities import is_platform_agent, render_handbook_discovery
from .instance import InstanceContext
from .operation_worker import SESSION_ACTION_KINDS
from .permissions import (
    ExecutionPermissions,
    actions_for_tools,
    execution_owner,
    resources_for_execution,
)
from .session_operations import (
    acknowledge_items,
    pending_items,
    unapplied_items,
)
from .secretary_contract import SECRETARY_CONTRACT
from .pi_reply import (
    LAUNCH_MODE_RESUME,
    LAUNCH_MODE_RUN,
    PiReplyHandler,
    PiReplyCommandError,
    PiRunProvenance,
)
from .profile_runtime import freeze_profile_handler
from .runtime_defaults import (
    ResolvedRuntimeDefaults,
    RuntimeDefaultsResolver,
)
from .secretary import REPLY_SOURCE, TurnInput
from .storage import (
    CORE_SYSTEM_AGENTS,
    AgentProfileRepository,
    ConversationRepository,
    ConversationRuntimeRepository,
    Database,
    OperationRepository,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PersistentConversationConfigurationError",
    "PersistentConversationError",
    "PersistentConversationHandler",
    "PersistentConversationSessionError",
]


def _session_action_payload(
    action_type: str, record: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the operation input for one legacy session action record."""
    if action_type == "trigger_commit_push":
        return {
            "push": bool(record.get("push", True)),
            "message": str(record.get("message") or "").strip(),
        }
    if action_type == "trigger_review":
        return {"instructions": str(record.get("instructions") or "").strip()}
    return {}


class PersistentConversationError(Exception):
    """Base class for persistent conversation handler failures."""


class PersistentConversationConfigurationError(PersistentConversationError):
    """Trusted handler configuration or pinned paths cannot be used.

    Raised before any model launch, for example when the persisted
    ``runner_state_dir`` is missing or is relative after ``expanduser``.
    """


class PersistentConversationSessionError(PersistentConversationError):
    """A successful call did not yield a usable native Runner session.

    Raised when the first ``run`` completes without a session id or when a
    resume binding has no pinned session id. It is never recovered into a
    fresh session.
    """


def _normalized_state_dir(value: Any) -> pathlib.Path:
    """Return the trusted Runner state directory after exact normalization.

    The Runner protocol requires ``state_dir`` to be a non-empty string that
    is absolute after ``expanduser`` and then resolved. A missing, non-string
    or relative value raises :class:`PersistentConversationConfigurationError`
    before any model launch. The returned resolved path is the exact value
    persisted for later resume.
    """
    if not isinstance(value, str) or not value.strip():
        raise PersistentConversationConfigurationError(
            "runner state_dir must be a non-empty string from the trusted "
            "role configuration"
        )
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        raise PersistentConversationConfigurationError(
            "runner state_dir must be an absolute path after expanduser"
        )
    return path.resolve()


def _read_role_config(role_config_path: pathlib.Path) -> dict[str, Any]:
    """Read one generated role JSON object, failing as a typed error.

    Both the pinned base role export and the per-call execution role are
    plain JSON objects. A missing, unreadable or non-object file is a trusted
    configuration failure, never a silent empty policy.
    """
    try:
        raw = role_config_path.read_text(encoding="utf-8")
        config = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PersistentConversationConfigurationError(
            "cannot read generated runner role configuration: "
            f"{type(error).__name__}: {error}"
        ) from error
    if not isinstance(config, dict):
        raise PersistentConversationConfigurationError(
            "generated runner role configuration must be a JSON object"
        )
    return config


def _role_config_state_dir(role_config_path: pathlib.Path) -> pathlib.Path:
    """Read and normalize ``state_dir`` from the generated role JSON.

    The generated, pinned role configuration is the artifact the Runner
    itself reads for a fresh ``run``. Persisting its ``state_dir`` keeps the
    later resume ``--state-dir`` argv identical to the trusted Runner policy.
    """
    return _normalized_state_dir(
        _read_role_config(role_config_path).get("state_dir")
    )


def _write_json(path: pathlib.Path, value: Any) -> None:
    """Write one generated JSON document durably as UTF-8."""
    data = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_execution_role(
    base_role_path: pathlib.Path,
    role_config: Mapping[str, Any],
    catalog_index_text: str | None = None,
) -> pathlib.Path:
    """Write one unique per-call execution-role document.

    The document is a copy of the pinned base role policy with the current
    resolved runtime selection applied. It lives beside the pinned export so
    the base ``runner_role.json`` and the profile snapshot stay untouched.
    The directory name is a random id, so a retry never overwrites an earlier
    call's execution role.
    """
    execution_dir = base_role_path.parent / "executions" / uuid.uuid4().hex
    execution_dir.mkdir(parents=True, exist_ok=False)
    role_copy = dict(role_config)
    if catalog_index_text:
        catalog_path = execution_dir / "knowledge_catalog.md"
        catalog_path.write_text(catalog_index_text.strip() + "\n", encoding="utf-8")
        instructions = list(role_copy.get("instructions") or [])
        instructions.append(str(catalog_path))
        role_copy["instructions"] = instructions

    execution_role_path = execution_dir / "runner_role.json"
    _write_json(execution_role_path, role_copy)
    return execution_role_path


@dataclasses.dataclass(frozen=True)
class _ExecutionContext:
    """Pinned, pre-launch state shared by every launch mode.

    ``base_role_path`` is the immutable pinned profile export that the
    generated execution role is copied from. ``session_id`` is ``None`` for a
    first ``run`` (including reuse of prepared paths after a pre-launch
    abort) and the native session id for a ``resume``.
    """

    binding: Any
    base_role_path: pathlib.Path
    artifact_dir: pathlib.Path
    state_dir: pathlib.Path
    launch_mode: str
    session_id: str | None


@dataclasses.dataclass(frozen=True)
class _PreparedExecution:
    """A resolved execution role and its ready-to-run child adapter."""

    handler: PiReplyHandler
    execution_role_path: pathlib.Path
    runtime_defaults: Mapping[str, Any]
    grant_token: str | None = None
    attachment_context: str | None = None
    attachment_manifest: Mapping[str, Any] | None = None


def _stored_provenance(
    execution: PiRunProvenance,
    binding: Any,
    launch_mode: str,
) -> dict[str, Any]:
    """Build the provenance mapping stored with a successful call.

    The adapter record already carries the turn/conversation/message ids,
    argv, launch mode, requested/returned session ids, artifact paths, the
    actual execution-role path and the resolved runtime-default evidence.
    This adds the pinned binding context so the stored record keeps the
    agent, revision, source and pinned base config paths even after the
    export directory is inspected later.
    """
    payload = execution.to_dict()
    payload.update(
        {
            "source": REPLY_SOURCE,
            "agent_id": binding.agent_id,
            "profile_revision": binding.profile_revision,
            "role_export_path": binding.role_export_path,
            "snapshot_export_path": binding.snapshot_export_path,
            "prompt_export_path": binding.prompt_export_path,
            "runner_state_dir": binding.runner_state_dir,
            "launch_mode": launch_mode,
            # The actual role handed to the Runner is the per-call execution
            # document, not the pinned base export.
            "execution_role_path": (
                str(execution.role_config_path)
                if execution.role_config_path is not None
                else None
            ),
        }
    )
    return payload


class PersistentConversationHandler:
    """Callable ``Handler`` that pins one conversation to one Runner session.

    Args:
        database: The file-backed
            :class:`~kuzya.storage.Database` that owns the runtime
            binding. The handler builds its own
            :class:`~kuzya.storage.ConversationRuntimeRepository`.
        agent_id: The registry identity selected for the conversation. On the
            first call the database pins that agent's current revision.
        runner_argv_prefix: Trusted Runner argument vector, passed unchanged
            to :class:`~kuzya.pi_reply.PiReplyHandler`.
        artifact_root: Parent directory for frozen profile exports. It is
            expanded with ``expanduser``. The per-conversation Runner state
            directory is never invented here: it is read from the generated
            pinned role configuration and only then persisted.
        attachments_root: Optional explicit root for the conversation/turn
            attachment store. When omitted it is
            ``<artifact_root>/attachments``; no host HOME path is used.
        env: Optional child environment; ``None`` inherits the caller's
            process environment unchanged.
        runtime_defaults: Optional injected
            :class:`~kuzya.runtime_defaults.RuntimeDefaultsResolver`.
            When omitted the handler reads the host global Pi settings
            (``~/.pi/agent/settings.json``) and no app TOML. Tests and manual
            operators inject a resolver bound to a temporary app TOML and/or
            settings file so the real host configuration is never read.
        defaults_config_path: Optional app TOML with a ``[default]`` table,
            used only when ``runtime_defaults`` is omitted. It supplies an
            explicit manual override above the host global Pi settings.

    The constructor only stores trusted configuration and performs no I/O,
    no database access and no child launch. Runtime defaults are resolved
    afresh on *every* new model call, including resume of an existing
    conversation, so editing a default file affects the next invocation
    without touching the pinned profile policy.
    """

    def __init__(
        self,
        database: Database,
        agent_id: str,
        *,
        runner_argv_prefix: Sequence[str],
        artifact_root: str | os.PathLike[str],
        attachments_root: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        runtime_defaults: RuntimeDefaultsResolver | None = None,
        defaults_config_path: str | os.PathLike[str] | None = None,
        knowledge_base: Any | None = None,
        instance: InstanceContext | None = None,
        permissions: ExecutionPermissions | None = None,
    ) -> None:
        prefix = tuple(runner_argv_prefix)
        if not prefix or any(
            not isinstance(item, str) or not item for item in prefix
        ):
            raise ValueError(
                "runner_argv_prefix must be a non-empty sequence of "
                "non-empty strings"
            )
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent_id must be a non-empty string")
        if runtime_defaults is not None and defaults_config_path is not None:
            raise ValueError(
                "pass either runtime_defaults or defaults_config_path, "
                "not both"
            )
        self._database = database
        self._agent_id = agent_id
        self._runner_argv_prefix = prefix
        self._artifact_root = pathlib.Path(artifact_root).expanduser()
        self._attachments = ConversationAttachmentStore(
            attachments_root
            if attachments_root is not None
            else self._artifact_root / "attachments"
        )
        self._env = dict(env) if env is not None else None
        self._knowledge_base = knowledge_base
        self._instance = instance
        self._permissions = permissions
        # Default source is the host global Pi settings with no app TOML.
        # The resolver holds no resolved values, so every call re-reads it.
        if runtime_defaults is not None:
            self._runtime_defaults = runtime_defaults
        else:
            self._runtime_defaults = RuntimeDefaultsResolver(
                app_config_path=defaults_config_path
            )
        self._repository = ConversationRuntimeRepository(database)
        self._conversations = ConversationRepository(database)
        self._last_execution: PiRunProvenance | Mapping[str, Any] | None = None

    @property
    def last_execution(
        self,
    ) -> PiRunProvenance | Mapping[str, Any] | None:
        """Result of the most recent call.

        A fresh child launch exposes the adapter's
        :class:`~kuzya.pi_reply.PiRunProvenance`. A cached recovery
        exposes the provenance ``Mapping`` stored on the binding, because no
        new child ran. ``None`` before the first call.
        """
        return self._last_execution

    def __call__(self, turn_input: TurnInput) -> str:
        """Reserve, resolve the selection, run or resume, and store a reply."""
        self._last_execution = None

        if self._knowledge_base is not None and hasattr(
            self._knowledge_base, "sync_agent_from_file"
        ):
            try:
                self._knowledge_base.sync_agent_from_file(
                    self._agent_id, self._database
                )
            except Exception:
                pass

        reservation = self._repository.reserve_call(
            conversation_id=turn_input.conversation_id,
            turn_id=turn_input.turn_id,
            attempt_count=turn_input.attempt_count,
            agent_id=self._agent_id,
        )
        if reservation.cached:
            # A stored successful result returns before any new defaults
            # resolution and never launches another child.
            return self._return_cached(reservation)

        ownership_token = reservation.ownership_token
        if ownership_token is None:  # pragma: no cover - reserved implies token
            raise PersistentConversationError(
                "conversation runtime reservation had no ownership token"
            )
        # Pre-launch context preparation: pin/export a fresh conversation or
        # reconstruct the persisted paths. These are existing fail-closed
        # configuration failures.
        try:
            context = self._prepare_context(
                turn_input, reservation.binding, ownership_token
            )
        except BaseException as error:
            self._block_failed_reservation(
                turn_input, ownership_token, error
            )
            raise
        # Per-call runtime-default resolution and execution-role generation.
        # A failure here is proven to occur before the child adapter exists,
        # so the reservation is released for an explicit retry instead of
        # permanently blocking the conversation.
        try:
            prepared = self._prepare_execution(context, turn_input)
        except BaseException as error:
            self._release_prelaunch(turn_input, ownership_token, error)
            raise
        # From here the failure can be after entering child execution, so it
        # remains fail-closed and blocked.
        try:
            return self._execute(
                turn_input, context, prepared, ownership_token
            )
        except BaseException as error:
            execution = prepared.handler.last_execution
            if (
                isinstance(error, PiReplyCommandError)
                and error.connection_error
                and execution is not None
                and execution.runner_status == "failed"
                and context.launch_mode == LAUNCH_MODE_RESUME
                and execution.returned_session_id == context.session_id
            ):
                # The child ended and identified the pinned native session.
                # Continue only with new input; never replay the failed call.
                self._repository.release_finished_failed_call(
                    conversation_id=turn_input.conversation_id,
                    ownership_token=ownership_token,
                    turn_id=turn_input.turn_id,
                    attempt_count=turn_input.attempt_count,
                )
                error.args = (f"{error}; conversation_continuation_available;",)
            else:
                self._block_failed_reservation(
                    turn_input, ownership_token, error
                )
            raise
        finally:
            # The execution-scoped credential lives exactly as long as the
            # child call. Revoking it never reopens anonymous access because
            # authentication mode is fixed at application construction.
            if prepared.grant_token and self._permissions is not None:
                self._permissions.revoke(prepared.grant_token)

    # -- cached ----------------------------------------------------------

    def _return_cached(self, reservation: Any) -> str:
        """Return a stored successful reply without launching a child."""
        reply_text = reservation.reply_text
        if not reply_text:
            raise PersistentConversationConfigurationError(
                "cached conversation result has no reply text"
            )
        self._last_execution = reservation.provenance
        return reply_text

    # -- reserved --------------------------------------------------------

    def _prepare_context(
        self,
        turn_input: TurnInput,
        binding: Any,
        ownership_token: str,
    ) -> _ExecutionContext:
        """Pin, export or reconstruct the paths used by this call.

        A fresh conversation freezes the pinned revision and persists the
        prepared paths before any runtime-defaults resolution, so a
        pre-launch abort leaves reusable paths for the first run. A binding
        with prepared paths but no native session reuses them for that first
        run. A binding with a native session resumes it.
        """
        if self._is_unprepared(binding):
            return self._prepare_fresh(turn_input, binding, ownership_token)
        self._require_complete_binding(binding)
        if binding.runner_session_id is None:
            # A prepared binding that never stored a result is a first run
            # interrupted by a safe pre-launch abort and may reuse its paths.
            # A binding that already stored a successful turn but lost its
            # native session id is corruption and must stay fail-closed; it
            # is never silently replaced by a fresh session.
            if binding.last_turn_id is None:
                return self._prepare_first_run_reuse(binding)
            raise PersistentConversationSessionError(
                "conversation runtime binding has no runner session id"
            )
        return self._prepare_resume(binding)

    @staticmethod
    def _bound_paths(binding: Any) -> tuple[Any, Any, Any, Any]:
        return (
            binding.role_export_path,
            binding.snapshot_export_path,
            binding.prompt_export_path,
            binding.runner_state_dir,
        )

    @classmethod
    def _is_unprepared(cls, binding: Any) -> bool:
        """Return whether the binding has no prepared context yet."""
        return all(value is None for value in cls._bound_paths(binding))

    @classmethod
    def _require_complete_binding(cls, binding: Any) -> None:
        """Raise a typed configuration error on partially attached paths."""
        if any(value is None for value in cls._bound_paths(binding)):
            raise PersistentConversationConfigurationError(
                "conversation runtime binding has partially attached paths"
            )

    def _prepare_fresh(
        self,
        turn_input: TurnInput,
        binding: Any,
        ownership_token: str,
    ) -> _ExecutionContext:
        """Freeze the pinned revision and persist the unresolved base context.

        Runtime defaults are deliberately not resolved here: the frozen
        export is the pinned base policy that every call copies and
        re-resolves, so editing a default file affects the next invocation
        of this conversation while the export and snapshot stay unchanged.
        """
        frozen = freeze_profile_handler(
            database=self._database,
            agent_id=self._agent_id,
            revision=binding.profile_revision,
            runner_argv_prefix=self._runner_argv_prefix,
            artifact_root=self._artifact_root,
            env=self._env,
        )
        state_dir = _role_config_state_dir(frozen.role_config_path)
        attached = self._repository.attach_prepared(
            conversation_id=turn_input.conversation_id,
            ownership_token=ownership_token,
            turn_id=turn_input.turn_id,
            attempt_count=turn_input.attempt_count,
            role_export_path=str(frozen.role_config_path),
            snapshot_export_path=str(frozen.snapshot_path),
            prompt_export_path=str(frozen.prompt_path),
            runner_state_dir=str(state_dir),
        )
        return _ExecutionContext(
            binding=attached,
            base_role_path=frozen.role_config_path,
            artifact_dir=frozen.runs_dir,
            state_dir=state_dir,
            launch_mode=LAUNCH_MODE_RUN,
            session_id=None,
        )

    def _prepare_first_run_reuse(self, binding: Any) -> _ExecutionContext:
        """Reuse prepared paths for the first run after a pre-launch abort.

        ``attach_prepared`` persists the pinned export before runtime defaults
        are resolved. If that resolution fails, the reservation is released
        and the paths remain, so the explicit retry of the same turn can run
        the first model call without creating a second export.
        """
        base_role_path = pathlib.Path(binding.role_export_path)
        return _ExecutionContext(
            binding=binding,
            base_role_path=base_role_path,
            artifact_dir=base_role_path.parent / "runs",
            state_dir=_normalized_state_dir(binding.runner_state_dir),
            launch_mode=LAUNCH_MODE_RUN,
            session_id=None,
        )

    def _prepare_resume(self, binding: Any) -> _ExecutionContext:
        """Reconstruct the pinned base context and native session id."""
        session_id = binding.runner_session_id
        if not session_id:
            raise PersistentConversationSessionError(
                "conversation runtime binding has no runner session id"
            )
        base_role_path = pathlib.Path(binding.role_export_path)
        return _ExecutionContext(
            binding=binding,
            base_role_path=base_role_path,
            artifact_dir=base_role_path.parent / "runs",
            state_dir=_normalized_state_dir(binding.runner_state_dir),
            launch_mode=LAUNCH_MODE_RESUME,
            session_id=session_id,
        )

    # -- per-call runtime selection --------------------------------------

    def _prepare_execution(
        self, context: _ExecutionContext, turn_input: TurnInput
    ) -> _PreparedExecution:
        """Resolve defaults, stage attachments and build the child adapter.

        This is the proven pre-launch boundary: reading the pinned base
        policy, resolving the injected defaults, staging the current turn's
        attachments and writing the unique execution role all happen before
        any child exists. The pinned base role file and the profile snapshot
        are never modified. Resume uses the execution role path with
        ``--agent`` while keeping the same native session and state directory.
        """
        attachments = self._attachments.prepare_turn(
            conversation_id=turn_input.conversation_id,
            turn_id=turn_input.turn_id,
            input_message_id=turn_input.input_message_id,
            metadata=turn_input.metadata,
        )
        base_role_config = _read_role_config(context.base_role_path)
        tools = list(base_role_config.get("tools") or [])
        platform_secretary = "create_task" in tools
        if platform_secretary and "resolve_workspace" not in tools:
            base_role_config["tools"] = [*tools, "resolve_workspace"]
        resolved = self._runtime_defaults.resolve(base_role_config)
        # The generated execution role points the Runner at the disposable
        # current-turn inbox view and the read-only conversation store. This
        # intentionally overrides any shared ``project/inbox`` fallback, so a
        # new conversation cannot inherit legacy workspace files.
        resolved.role_config["inbox_dir"] = str(attachments.inbox_dir)
        resolved.role_config["attachments_dir"] = str(
            attachments.store_root
        )
        # Capture provenance before the per-instance additions so the scoped
        # backend credential is never written into the stored provenance.
        runtime_defaults = resolved.to_provenance()
        grant_token: str | None = None
        # Explicit per-instance mounts and an execution-scoped backend token.
        if self._instance is not None:
            resolved_role = resolved.role_config
            if self._instance.knowledge_root.is_dir():
                resolved_role["knowledge_dir"] = str(
                    self._instance.knowledge_root
                )
            if self._instance.skills_root.is_dir():
                resolved_role["skills_dir"] = str(self._instance.skills_root)
            if self._permissions is not None and self._instance.backend_url:
                exec_actions = actions_for_tools(
                    resolved_role.get("tools") or []
                )
                grant = self._permissions.issue(
                    agent_id=self._agent_id,
                    actions=exec_actions,
                    resources=resources_for_execution(exec_actions),
                    label=f"conversation:{context.binding.conversation_id}",
                )
                grant_token = grant.token
                resolved_role["backend"] = self._instance.child_backend(
                    grant.token
                )
                resolved_role["backend"]["conversation_id"] = turn_input.conversation_id
        catalog_text = None
        if self._knowledge_base is not None and hasattr(
            self._knowledge_base, "get_catalog_index"
        ):
            try:
                catalog_text = self._knowledge_base.get_catalog_index()
            except Exception:
                pass
        if platform_secretary:
            catalog_text = SECRETARY_CONTRACT + "\n\n" + (catalog_text or "")
        discovery_tools = list(resolved.role_config.get("tools") or [])
        if is_platform_agent(self._agent_id, discovery_tools):
            discovery = render_handbook_discovery(discovery_tools)
            catalog_text = (
                f"{catalog_text}\n\n{discovery}" if catalog_text else discovery
            )
        execution_role_path = _write_execution_role(
            context.base_role_path, resolved.role_config, catalog_text
        )
        handler = self._build_runtime_handler(
            context, execution_role_path, runtime_defaults
        )
        return _PreparedExecution(
            handler=handler,
            execution_role_path=execution_role_path,
            runtime_defaults=runtime_defaults,
            grant_token=grant_token,
            attachment_context=attachments.prompt(),
            attachment_manifest=attachments.metadata(),
        )

    def _build_runtime_handler(
        self,
        context: _ExecutionContext,
        execution_role_path: pathlib.Path,
        runtime_defaults: Mapping[str, Any],
    ) -> PiReplyHandler:
        """Build an adapter over the execution role, not the base export."""
        resume = context.launch_mode == LAUNCH_MODE_RESUME
        return PiReplyHandler(
            runner_argv_prefix=self._runner_argv_prefix,
            role_config_path=execution_role_path,
            artifact_dir=context.artifact_dir,
            env=self._env,
            profile_agent_id=context.binding.agent_id,
            profile_revision=context.binding.profile_revision,
            profile_snapshot_path=context.binding.snapshot_export_path,
            resume_session_id=context.session_id if resume else None,
            runner_state_dir=str(context.state_dir) if resume else None,
            runtime_defaults_provenance=dict(runtime_defaults),
        )

    def _execute(
        self,
        turn_input: TurnInput,
        context: _ExecutionContext,
        prepared: _PreparedExecution,
        ownership_token: str,
    ) -> str:
        """Invoke the child once, then store the successful native session."""
        handler = prepared.handler
        metadata = dict(turn_input.metadata)
        conversation = self._conversations.get_conversation(
            turn_input.conversation_id
        )
        if conversation is not None:
            conversation_metadata = dict(conversation.meta or {})
            for key in ("task_id", "task_number", "task_title"):
                if key not in metadata and key in conversation_metadata:
                    metadata[key] = conversation_metadata[key]
        if prepared.attachment_context is not None:
            metadata["attachment_context"] = prepared.attachment_context
            if prepared.attachment_manifest is not None:
                metadata["attachment_manifest"] = prepared.attachment_manifest
        effective_input = (
            dataclasses.replace(turn_input, metadata=metadata)
            if metadata != dict(turn_input.metadata)
            else turn_input
        )

        try:
            reply_text = handler(effective_input)
        finally:
            # The adapter records provenance for failures too; expose the
            # actual record instead of ``None`` or an earlier success.
            self._last_execution = handler.last_execution
        execution = handler.last_execution
        if execution is None:  # pragma: no cover - adapter always records
            raise PersistentConversationSessionError(
                "pi runner produced no execution record"
            )
        returned_session_id = execution.returned_session_id
        if not returned_session_id:
            raise PersistentConversationSessionError(
                "pi runner completed without a returned session id"
            )

        session_pi_dir = context.state_dir / "sessions" / returned_session_id / "pi"

        # The tool writes one record per explicit invocation, tagged with the
        # turn identity the Runner exposed in /session/turn.json. Only the
        # current turn may deliver them: a resumed turn must not replay a
        # record that an earlier turn already requested. Legacy records
        # without a turn identity are left untouched and never replayed.
        attachment_items: list[tuple[int, dict]] = []
        attachments_file = session_pi_dir / "attachments.json"
        if attachments_file.is_file():
            try:
                attachment_items = pending_items(
                    attachments_file,
                    match=lambda record: isinstance(record, dict)
                    and record.get("turn_id") == turn_input.turn_id,
                )
                for _, att in attachment_items:
                    target = att.get("target")
                    if isinstance(target, str) and target:
                        file_tag = f"[SEND_FILE: {target}]"
                        if file_tag not in reply_text:
                            reply_text += f"\n\n{file_tag}"
            except Exception as e:
                logger.warning("Failed to process attachments: %s", e)

        self._repository.finish_call(
            conversation_id=turn_input.conversation_id,
            ownership_token=ownership_token,
            turn_id=turn_input.turn_id,
            attempt_count=turn_input.attempt_count,
            reply_text=reply_text,
            runner_session_id=returned_session_id,
            provenance=_stored_provenance(
                execution, context.binding, context.launch_mode
            ),
        )
        self._last_execution = execution
        # Acknowledge only after the reply is durable. If finish_call or the
        # ownership check fails, the records stay pending so a retry of the
        # same turn can still deliver them.
        if attachment_items:
            try:
                acknowledge_items(attachments_file, attachment_items)
            except Exception as e:
                logger.warning(
                    "Failed to acknowledge delivered attachments: %s", e
                )

        # If Pi session created new agents, persist them into repository and knowledge base
        created_agents_file = session_pi_dir / "created_agents.json"
        if created_agents_file.is_file():
            try:
                agents_data = unapplied_items(created_agents_file)
                agent_repo = AgentProfileRepository(self._database)
                for ag in agents_data:
                    a_id = ag.get("agent_id")
                    a_prompt = ag.get("system_prompt")
                    if not a_id or not a_prompt:
                        continue
                    current_prof = agent_repo.get_current(a_id)
                    # A legacy create is not an upsert and must not revert later edits.
                    if current_prof is not None:
                        if self._knowledge_base is not None:
                            self._knowledge_base.save_agent_profile(current_prof)
                        continue
                    tools = ag.get("tools") or ["read", "write", "edit", "bash"]
                    skills = ag.get("skills") or []
                    skill_names = [s if isinstance(s, str) else s.get("skill") for s in skills if s]
                    skill_bindings = [
                        {"skill": s} if isinstance(s, str) else s
                        for s in skills
                        if s
                    ]
                    runner_cfg = {"tools": tools}
                    # Explicit per-instance state/workspace for a newly created
                    # agent. Without an instance context the profile keeps no
                    # host-HOME path and the worker supplies a state dir later.
                    if self._instance is not None:
                        agent_state = self._instance.state_root / a_id
                        agent_ws = self._instance.workspaces_root / a_id
                        agent_state.mkdir(parents=True, exist_ok=True)
                        agent_ws.mkdir(parents=True, exist_ok=True)
                        runner_cfg["state_dir"] = str(agent_state)
                        runner_cfg["project"] = str(agent_ws)
                    if skill_names:
                        runner_cfg["skills"] = skill_names
                    prof = agent_repo.create(
                        agent_id=a_id,
                        name=ag.get("name") or a_id,
                        role=ag.get("role") or "developer",
                        system_prompt=a_prompt,
                        runner_config=runner_cfg,
                        skills=skill_bindings,
                    )
                    if self._knowledge_base is not None and hasattr(
                        self._knowledge_base, "save_agent_profile"
                    ):
                        self._knowledge_base.save_agent_profile(prof)
            except Exception as e:
                logger.warning("Failed to persist created agents from session: %s", e)

        archived_agents_file = session_pi_dir / "archived_agents.json"
        if archived_agents_file.is_file():
            try:
                for ag in unapplied_items(archived_agents_file):
                    a_id = ag.get("agent_id")
                    if not a_id or a_id in CORE_SYSTEM_AGENTS:
                        continue
                    if self._knowledge_base is not None and hasattr(
                        self._knowledge_base, "archive_agent"
                    ):
                        self._knowledge_base.archive_agent(a_id, self._database)
                    else:
                        repo = AgentProfileRepository(self._database)
                        cur = repo.get_current(a_id)
                        if cur is not None:
                            repo.archive(a_id, expected_revision=cur.revision)
                    logger.info("Archived agent %s from session", a_id)
            except Exception as e:
                logger.warning("Failed to archive agents from session: %s", e)

        saved_projects_file = session_pi_dir / "saved_projects.json"
        if saved_projects_file.is_file() and self._knowledge_base is not None:
            try:
                for prj in unapplied_items(saved_projects_file):
                    p_name = prj.get("name")
                    p_sum = prj.get("summary")
                    if p_name and p_sum:
                        self._knowledge_base.create_or_update_project_summary(p_name, p_sum)
                        logger.info("Persisted project summary for %s from session", p_name)
            except Exception as e:
                logger.warning("Failed to persist saved projects from session: %s", e)

        # Process written knowledge items
        written_kb_file = session_pi_dir / "written_knowledge.json"
        if written_kb_file.is_file() and self._knowledge_base is not None:
            try:
                kb_entries = unapplied_items(written_kb_file)
                kb_root = self._knowledge_base.root_dir
                for item in kb_entries:
                    cat = item.get("category", "docs")
                    name = item.get("name")
                    content = item.get("content")
                    if name and content:
                        dest = (kb_root / cat / name).resolve()
                        if not dest.is_relative_to(kb_root.resolve()):
                            raise ValueError("Knowledge path escapes the knowledge base")
                        if dest.is_relative_to(self._knowledge_base.agents_dir.resolve()):
                            logger.warning("Ignoring legacy agent document write; use the profile API")
                            continue
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if not dest.is_file() or dest.read_text(encoding="utf-8") != content:
                            dest.write_text(content, encoding="utf-8")
                        logger.info("Saved knowledge item to %s", dest)
                if hasattr(self._knowledge_base, "git_commit"):
                    self._knowledge_base.git_commit("Update knowledge base from secretary session")
            except Exception as e:
                logger.warning("Failed to persist written knowledge from session: %s", e)

        # Process skill installation and creation
        skills_base = (
            self._instance.skills_root if self._instance is not None else None
        )
        install_skills_file = session_pi_dir / "install_skills.json"
        if install_skills_file.is_file() and skills_base is None:
            logger.warning(
                "Ignoring skill installation: no instance skills root configured"
            )
        elif install_skills_file.is_file() and skills_base is not None:
            try:
                import shutil as _shutil
                skills_base.mkdir(parents=True, exist_ok=True)
                for sk in unapplied_items(install_skills_file):
                    s_name = sk.get("name")
                    src = sk.get("source")
                    if s_name and src:
                        src_path = pathlib.Path(src).expanduser().resolve()
                        if src_path.is_dir():
                            target = skills_base / s_name
                            if target.exists():
                                _shutil.rmtree(target)
                            _shutil.copytree(src_path, target)
                            logger.info("Installed skill %s from %s", s_name, src_path)
            except Exception as e:
                logger.warning("Failed to install skills from session: %s", e)

        created_skills_file = session_pi_dir / "created_skills.json"
        if created_skills_file.is_file() and skills_base is None:
            logger.warning(
                "Ignoring skill creation: no instance skills root configured"
            )
        elif created_skills_file.is_file() and skills_base is not None:
            try:
                skills_base.mkdir(parents=True, exist_ok=True)
                for c_sk in unapplied_items(created_skills_file):
                    c_name = c_sk.get("name")
                    skill_md = c_sk.get("skill_md")
                    scripts = c_sk.get("scripts") or {}
                    if c_name and skill_md:
                        sk_dir = skills_base / c_name
                        sk_dir.mkdir(parents=True, exist_ok=True)
                        (sk_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
                        for s_file, s_body in scripts.items():
                            s_dest = sk_dir / s_file
                            s_dest.parent.mkdir(parents=True, exist_ok=True)
                            s_dest.write_text(s_body, encoding="utf-8")
                        logger.info("Created skill %s in %s", c_name, sk_dir)
            except Exception as e:
                logger.warning("Failed to create skills from session: %s", e)

        # Process triggered pipeline actions through the durable operation queue.
        # The action body is acknowledged only after the idempotent submit
        # succeeds, so a restart never replays a completed side effect and a
        # crash after submit reuses the same idempotency key.
        actions_file = session_pi_dir / "actions.json"
        if actions_file.is_file():
            try:
                operation_repository = OperationRepository(self._database)
                for act in unapplied_items(actions_file):
                    action_type = act.get("action")
                    t_id = act.get("task_id")
                    kind = SESSION_ACTION_KINDS.get(action_type)
                    if not t_id or kind is None:
                        if action_type is not None:
                            logger.warning(
                                "Ignoring unknown session action %r", action_type
                            )
                        continue
                    payload = _session_action_payload(action_type, act)
                    idempotency_key = hashlib.sha256(
                        json.dumps(
                            [
                                turn_input.conversation_id,
                                returned_session_id,
                                action_type,
                                t_id,
                                payload,
                            ],
                            sort_keys=True,
                            ensure_ascii=False,
                        ).encode("utf-8")
                    ).hexdigest()
                    operation_repository.submit(
                        kind=kind,
                        target_id=t_id,
                        input=payload,
                        idempotency_key=idempotency_key,
                        metadata={
                            "source": REPLY_SOURCE,
                            "session_action": action_type,
                            "conversation_id": turn_input.conversation_id,
                        },
                        owner=execution_owner(
                            self._agent_id,
                            f"conversation:{turn_input.conversation_id}",
                        ),
                    )
                    logger.info(
                        "Queued %s as durable operation kind=%s task=%s",
                        action_type,
                        kind,
                        t_id,
                    )
            except Exception as e:
                logger.warning("Failed to queue session actions: %s", e)

        return reply_text

    # -- fail closed -----------------------------------------------------

    def _block_failed_reservation(
        self,
        turn_input: TurnInput,
        ownership_token: str,
        error: BaseException,
    ) -> None:
        """Release the reservation into the blocked latch, never unlocking."""
        try:
            self._repository.block_call(
                conversation_id=turn_input.conversation_id,
                ownership_token=ownership_token,
                turn_id=turn_input.turn_id,
                attempt_count=turn_input.attempt_count,
                reason=f"{type(error).__name__}: {error}",
            )
        except Exception as block_error:  # noqa: BLE001 - original wins
            error.add_note(
                "conversation runtime block failed: "
                f"{type(block_error).__name__}: {block_error}"
            )

    def _release_prelaunch(
        self,
        turn_input: TurnInput,
        ownership_token: str,
        error: BaseException,
    ) -> None:
        """Release an owned reservation after a proven pre-launch failure.

        Used only for failures before the child adapter is invoked. It never
        sets the blocked latch, so an operator can edit the malformed
        runtime-default file and explicitly retry the failed turn without a
        duplicate model call.
        """
        try:
            self._repository.release_prelaunch_call(
                conversation_id=turn_input.conversation_id,
                ownership_token=ownership_token,
                turn_id=turn_input.turn_id,
                attempt_count=turn_input.attempt_count,
            )
        except Exception as release_error:  # noqa: BLE001 - original wins
            error.add_note(
                "conversation runtime pre-launch release failed: "
                f"{type(release_error).__name__}: {release_error}"
            )

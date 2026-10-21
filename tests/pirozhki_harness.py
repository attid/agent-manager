"""Isolated Pirozhki acceptance harness.

The harness composes the real production components against a disposable
instance: a migrated temporary SQLite database, a temporary Git-backed
knowledge base, temporary workspaces and artifacts, the real secretary
dispatcher, task worker and durable operation worker, and a real HTTP backend
served by uvicorn on an ephemeral loopback port.

No model, network, production path or credential is involved. Child
executions are the deterministic ``tests/fake_pi_runner.py`` process, and the
real ``src/pi_runner/policy.mjs`` JavaScript tools are executed by Node
against the same temporary backend.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Iterator

import httpx
import uvicorn

from kuzya.api import create_app
from kuzya.dispatcher import QueueDispatcher
from kuzya.instance import InstanceContext
from kuzya.knowledge import KnowledgeBase
from kuzya.operation_handlers import register_default_operation_handlers
from kuzya.operation_worker import (
    OperationInstanceContext,
    OperationWorker,
)
from kuzya.permissions import ExecutionPermissions
from kuzya.persistent_conversation import PersistentConversationHandler
from kuzya.runtime_defaults import RuntimeDefaultsResolver
from kuzya.storage import (
    AgentProfileRepository,
    Database,
    OperationRepository,
    TaskRepository,
)
from kuzya.task_worker import TaskExecutionWorker

ADMIN_TOKEN = "pirozhki-admin-token"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
FAKE_RUNNER = pathlib.Path(__file__).with_name("fake_pi_runner.py")
JS_HARNESS = pathlib.Path(__file__).with_name("e2e_pirozhki_tools.mjs")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def init_git_repo(path: pathlib.Path) -> None:
    """Initialize a deterministic local Git repository with identity."""
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-q", "-b", "main")
    run_git(path, "config", "user.email", "pirozhki-e2e@example.invalid")
    run_git(path, "config", "user.name", "Pirozhki E2E")
    run_git(path, "config", "commit.gpgsign", "false")


class PirozhkiInstance:
    """One disposable, fully wired Kuzya instance."""

    def __init__(
        self,
        *,
        root: pathlib.Path,
        database: Database,
        knowledge_base: KnowledgeBase,
        instance_context: InstanceContext,
        permissions: ExecutionPermissions,
        dispatcher: QueueDispatcher,
        task_worker: TaskExecutionWorker,
        operation_worker: OperationWorker,
        project_workspace: pathlib.Path,
        base_url: str,
        server: uvicorn.Server,
        thread: threading.Thread,
        sock: socket.socket,
    ) -> None:
        self.root = root
        self.database = database
        self.knowledge_base = knowledge_base
        self.instance_context = instance_context
        self.permissions = permissions
        self.dispatcher = dispatcher
        self.task_worker = task_worker
        self.operation_worker = operation_worker
        self.project_workspace = project_workspace
        self.base_url = base_url
        self._server = server
        self._thread = thread
        self._socket = sock
        self.tasks = TaskRepository(database)
        self.operations = OperationRepository(database)
        self.agents = AgentProfileRepository(database)
        self.http = httpx.Client(
            base_url=base_url, headers=ADMIN_HEADERS, timeout=30.0
        )
        self._js_root = root / "js"
        self._js_root.mkdir(parents=True, exist_ok=True)
        self._js_session = root / "js-session"
        self._js_session.mkdir(parents=True, exist_ok=True)
        self._js_node = shutil.which("node")
        self._js_policy = self._js_root / "policy.mjs"
        if self._js_node is not None:
            shutil.copyfile(REPO_ROOT / "src/pi_runner/policy.mjs", self._js_policy)

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self.http.close()
        self._server.should_exit = True
        try:
            self._thread.join(10)
        finally:
            with contextlib.suppress(Exception):
                self._socket.close()

    def __enter__(self) -> "PirozhkiInstance":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- HTTP helpers ----------------------------------------------------

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.post(path, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.get(path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.patch(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.http.delete(path, **kwargs)

    def run_until_idle(self, limit: int = 12) -> list[Any]:
        """Drive the durable operation worker until the queue is empty."""
        outcomes: list[Any] = []
        for _ in range(limit):
            outcome = self.operation_worker.step()
            if outcome is None:
                break
            outcomes.append(outcome)
        else:  # pragma: no cover - defensive bound
            raise AssertionError("operation queue did not drain")
        return outcomes

    def set_analyzer_evidence(self, task_id: str) -> None:
        """Point every analyzer recommendation at the real target task.

        The deterministic fixture analyzer cannot read the prompt, so the
        acceptance gives it the concrete task identity before the analysis
        operation runs. Only the trusted profile registry is updated; the
        selection still flows through the production analyzer runner.
        """
        profile = self.agents.get_current("system-log-analyzer")
        assert profile is not None
        config = dict(profile.runner_config or {})
        report = json.loads(config["summary_text"])
        for recommendation in report["recommendations"]:
            recommendation["evidence"] = [f"task:{task_id}"]
        config["summary_text"] = json.dumps(report, ensure_ascii=False)
        self.agents.update(
            agent_id="system-log-analyzer",
            expected_revision=profile.revision,
            runner_config=config,
        )

    def run_js_tool(self, phase: str, argument: str | None = None) -> dict[str, Any]:
        """Execute the real JavaScript platform tools and return their JSON."""
        if self._js_node is None:
            raise AssertionError("Node.js is required for the JS tool acceptance")
        node_modules = self._js_root / "node_modules"
        if not (node_modules / "typebox").exists():
            releases = pathlib.Path.home() / ".pi/agent/install/releases"
            typebox = next(
                iter(sorted(releases.glob("*/node_modules/typebox"), reverse=True)),
                None,
            )
            if typebox is None:
                raise AssertionError("Pi TypeBox runtime is required")
            node_modules.mkdir(parents=True, exist_ok=True)
            (node_modules / "typebox").symlink_to(
                typebox, target_is_directory=True
            )
        harness = self._js_root / "e2e_pirozhki_tools.mjs"
        shutil.copyfile(JS_HARNESS, harness)
        argv = [
            self._js_node,
            str(harness),
            str(self._js_policy),
            str(self._js_session),
            self.base_url,
            str(self.project_workspace),
            phase,
        ]
        if argument is not None:
            argv.append(argument)
        env = dict(os.environ)
        env["E2E_ADMIN_TOKEN"] = ADMIN_TOKEN
        result = subprocess.run(
            argv,
            cwd=str(self._js_root),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"JS tool phase {phase!r} failed:\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert lines, f"no JSON output from JS tool phase {phase!r}"
        return json.loads(lines[-1])


def _seed_deterministic_runner_configs(
    database: Database, knowledge_base: KnowledgeBase
) -> None:
    """Point the seeded system profiles at the deterministic fake Runner.

    ``KnowledgeBase.initialize`` creates the real default profiles. The E2E
    then records only the deterministic executor selection (provider/model so
    the host Pi settings are never consulted) and the fixture's fixed output
    on those profiles, exporting the profile back to the knowledge base so a
    later file sync does not revert it.
    """
    repository = AgentProfileRepository(database)
    overrides = {
        "system-secretary": {
            "provider": "test-provider",
            "model": "test-model",
            "mode": "success",
            "session_id_value": "pirozhki-secretary-session",
            "summary_text": (
                "Принял запрос. Задача на разработку pirozhki-shop поставлена "
                "через инструмент create_task."
            ),
        },
        "system-code-reviewer": {
            "engine": "pi",
            "provider": "test-provider",
            "model": "test-model",
            "mode": "success",
            "session_id_value": "pirozhki-reviewer-session",
            "summary_text": (
                '{"verdict": "approved", "findings": []}'
            ),
        },
        "system-log-analyzer": {
            "provider": "test-provider",
            "model": "test-model",
            "mode": "success",
            "session_id_value": "pirozhki-analyzer-session",
            "summary_text": json.dumps(
                {
                    "summary": (
                        "Сессия показала, что для pirozhki-shop не хватало "
                        "описания точки входа и явного правила кнопки заказа."
                    ),
                    "findings": [
                        {
                            "kind": "missing_knowledge",
                            "detail": "Точка входа проекта не описана.",
                        }
                    ],
                    "recommendations": [
                        {
                            "target_kind": "project",
                            "target_id": "pirozhki-shop",
                            "title": "Обновить описание проекта",
                            "rationale": "Саммари не содержит точки входа.",
                            "evidence": [],
                            "proposed_content": (
                                "# Проект: pirozhki-shop\n\n"
                                "- **Точка входа**: index.html\n"
                                "- **Кнопка заказа**: зелёная, текст "
                                "«Заказать горячий пирожок»\n"
                            ),
                        },
                        {
                            "target_kind": "profile",
                            "target_id": "pirozhki-developer",
                            "title": "Уточнить промпт разработчика",
                            "rationale": (
                                "Правило кнопки заказа стоит хранить в промпте."
                            ),
                            "evidence": [],
                            "proposed_content": {
                                "system_prompt": (
                                    "Ты разработчик pirozhki-shop. "
                                    "Кнопка заказа всегда зелёная и подписана "
                                    "«Заказать горячий пирожок»."
                                )
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            ),
        },
    }
    for agent_id, extra in overrides.items():
        profile = repository.get_current(agent_id)
        assert profile is not None, f"default profile missing: {agent_id}"
        config = dict(profile.runner_config or {})
        config.update(extra)
        updated = repository.update(
            agent_id=agent_id,
            expected_revision=profile.revision,
            runner_config=config,
        )
        knowledge_base.save_agent_profile(updated)


def build_pirozhki_instance(
    migrated_db: pathlib.Path,
    root: pathlib.Path,
    *,
    runner_argv_prefix: tuple[str, ...] | None = None,
    seed_deterministic_configs: bool = True,
    runtime_defaults: RuntimeDefaultsResolver | None = None,
) -> PirozhkiInstance:
    """Build and start one isolated acceptance instance.

    ``runner_argv_prefix`` defaults to the deterministic fake Runner. The
    opt-in real-model acceptance passes the production ``pi_runner`` prefix
    and disables the deterministic profile overrides so the real system
    profiles and host runtime selection are exercised.

    ``runtime_defaults`` overrides the model/provider/auth resolver. The
    deterministic tests pass an explicitly absent settings path so the host
    Pi settings can never influence them; the real-model acceptance supplies
    the real host resolution (or an explicitly selected provider/model).
    """
    runner_prefix = (
        tuple(runner_argv_prefix)
        if runner_argv_prefix is not None
        else (sys.executable, str(FAKE_RUNNER))
    )
    root.mkdir(parents=True, exist_ok=True)
    knowledge_root = root / "knowledge"
    workspaces_root = root / "workspaces"
    skills_root = root / "skills"
    state_root = root / "state"
    artifact_root = root / "artifacts"
    project_workspace = workspaces_root / "pirozhki-shop"
    for directory in (
        knowledge_root,
        workspaces_root,
        skills_root,
        state_root,
        artifact_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    init_git_repo(knowledge_root)
    init_git_repo(project_workspace)
    (project_workspace / "index.html").write_text(
        '<!doctype html>\n<html><body><button id="order">Заказать</button></body></html>\n',
        encoding="utf-8",
    )
    run_git(project_workspace, "add", "--", "index.html")
    run_git(project_workspace, "commit", "-q", "-m", "Initial pirozhki-shop page")

    database = Database.from_path(migrated_db)
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    sock: socket.socket | None = None
    try:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        backend_url = f"http://127.0.0.1:{port}"

        permissions = ExecutionPermissions()
        instance_context = InstanceContext(
            state_root=state_root,
            workspaces_root=workspaces_root,
            skills_root=skills_root,
            knowledge_root=knowledge_root,
            artifact_root=artifact_root,
            default_workspace=project_workspace,
            backend_url=backend_url,
        )
        knowledge_base = KnowledgeBase(knowledge_root, instance=instance_context)
        knowledge_base.initialize(database=database)
        if seed_deterministic_configs:
            _seed_deterministic_runner_configs(database, knowledge_base)

        defaults = runtime_defaults or RuntimeDefaultsResolver(
            global_settings_path=root / "absent-pi-settings.json"
        )
        secretary_handler = PersistentConversationHandler(
            database,
            "system-secretary",
            runner_argv_prefix=list(runner_prefix),
            artifact_root=artifact_root / "conversations",
            knowledge_base=knowledge_base,
            instance=instance_context,
            permissions=permissions,
            runtime_defaults=defaults,
        )
        dispatcher = QueueDispatcher(
            database, handler=secretary_handler, poll_interval_seconds=0.01
        )
        task_worker = TaskExecutionWorker(
            database=database,
            artifact_root=artifact_root / "tasks",
            agent_id="kuzya-developer",
            runner_argv_prefix=list(runner_prefix),
            default_workspace=str(project_workspace),
            knowledge_base=knowledge_base,
            runtime_defaults=defaults,
            instance=instance_context,
            permissions=permissions,
        )
        operation_instance = OperationInstanceContext(
            database=database,
            artifact_root=artifact_root / "operations",
            runner_argv_prefix=runner_prefix,
            default_workspace=project_workspace,
            knowledge_base=knowledge_base,
            instance=instance_context,
            permissions=permissions,
            runtime_defaults=defaults,
        )
        operation_worker = OperationWorker(database, instance=operation_instance)
        register_default_operation_handlers(operation_worker)

        app = create_app(
            f"sqlite:///{migrated_db}",
            api_tokens={ADMIN_TOKEN},
            default_workspace=project_workspace,
            knowledge_base=knowledge_base,
            permissions=permissions,
        )
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 10
        while (
            not server.started
            and thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not server.started:
            raise AssertionError("isolated HTTP backend did not start")

        return PirozhkiInstance(
            root=root,
            database=database,
            knowledge_base=knowledge_base,
            instance_context=instance_context,
            permissions=permissions,
            dispatcher=dispatcher,
            task_worker=task_worker,
            operation_worker=operation_worker,
            project_workspace=project_workspace,
            base_url=backend_url,
            server=server,
            thread=thread,
            sock=sock,
        )
    except BaseException:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(10)
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()
        database.dispose()
        raise


@contextlib.contextmanager
def pirozhki_instance(
    migrated_db: pathlib.Path,
    root: pathlib.Path,
    *,
    runner_argv_prefix: tuple[str, ...] | None = None,
    seed_deterministic_configs: bool = True,
    runtime_defaults: RuntimeDefaultsResolver | None = None,
) -> Iterator[PirozhkiInstance]:
    instance = build_pirozhki_instance(
        migrated_db,
        root,
        runner_argv_prefix=runner_argv_prefix,
        seed_deterministic_configs=seed_deterministic_configs,
        runtime_defaults=runtime_defaults,
    )
    try:
        yield instance
    finally:
        instance.close()
        instance.database.dispose()


__all__ = [
    "ADMIN_HEADERS",
    "ADMIN_TOKEN",
    "PirozhkiInstance",
    "build_pirozhki_instance",
    "init_git_repo",
    "pirozhki_instance",
    "run_git",
]

"""Knowledge base management for projects, components, and agent profiles."""

from __future__ import annotations

import logging
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from .instance import InstanceContext
from .secretary_contract import SECRETARY_CONTRACT

from agent_manager.storage import (
    CORE_SYSTEM_AGENTS,
    AgentProfile,
    AgentProfileRepository,
    Database,
)

logger = logging.getLogger(__name__)

DEFAULT_KNOWLEDGE_DIR = Path.home() / ".local/share/agent-manager/knowledge"
SEND_FILE_PATTERN = re.compile(
    r"\[(?:SEND_FILE|ATTACH_FILE|FILE):\s*([^\]]+)\]", re.IGNORECASE
)


class KnowledgePathError(ValueError):
    """A requested knowledge target would escape its instance-owned subtree."""


class KnowledgeProtectedPathError(KnowledgePathError):
    """A knowledge path belongs to a subtree with an API-managed lifecycle."""


class KnowledgeDocumentNotFoundError(FileNotFoundError):
    """A requested knowledge document does not exist."""


def contained_target_path(
    base_dir: str | os.PathLike[str],
    target_id: str,
    filename: str,
) -> Path:
    """Resolve ``base_dir/target_id/filename`` inside ``base_dir``.

    ``target_id`` may be a broad nested name; it is accepted as long as the
    resolved path stays inside ``base_dir``. Absolute paths, ``..``
    traversal and symlinks that point outside the subtree are rejected with
    :class:`KnowledgePathError`. This is the single resolver shared by
    recommendation preview, evidence hashing and the final knowledge write.
    """
    base = Path(base_dir).expanduser().resolve()
    candidate = (base / target_id.strip() / filename).resolve()
    if not candidate.is_relative_to(base):
        raise KnowledgePathError(
            f"knowledge target {target_id!r} is outside {base}"
        )
    return candidate


#: Repository ``docs/handbook`` directory copied into the knowledge root so a
#: child execution can read the platform contract read-only at
#: ``/knowledge/handbook``. Absent in an installed package: the sync is a
#: no-op and the injected discovery still names the stable repository paths.
DEFAULT_HANDBOOK_SOURCE = (
    Path(__file__).resolve().parents[2] / "docs" / "handbook"
)


import shutil


def get_default_pi_runtime() -> tuple[list[str], list[str]]:
    """Return default (pi_command, runtime_paths) for the host environment.

    The Pi installation is discovered from the current interpreter's ``HOME``
    only as a runtime dependency. There is no compiled-in production path
    fallback: when Node is absent the caller sees a clear failure instead of
    a host-specific path.
    """
    node_exe = shutil.which("node")
    if node_exe is None:
        raise RuntimeError("Node.js executable not found on PATH")
    node_path = Path(node_exe).resolve()

    release_dir = Path.home() / ".pi/agent/install/releases/1.3.0"
    cli_js = release_dir / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    if not cli_js.is_file():
        releases = sorted((Path.home() / ".pi/agent/install/releases").glob("*"))
        if releases:
            release_dir = releases[-1]
            cli_js = release_dir / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"

    pi_command = [str(node_path), str(cli_js)]
    runtime_paths = [str(node_path), str(release_dir)]

    uv_bin = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    if Path(uv_bin).is_file():
        runtime_paths.append(str(Path(uv_bin).resolve()))

    py_dir = Path.home() / ".local/share/uv/python/cpython-3.14.7-linux-x86_64-gnu"
    if py_dir.is_dir():
        runtime_paths.append(str(py_dir))

    return pi_command, runtime_paths


def format_agent_profile_markdown(profile: AgentProfile) -> str:
    """Render an immutable AgentProfile as a clean, standardized Markdown document."""
    tools: list[str] = []
    if isinstance(profile.runner_config, dict):
        tools = list(profile.runner_config.get("tools") or [])

    policy = {
        key: profile.runner_config[key]
        for key in (
            "project",
            "workdir",
            "project_read_only",
            "network",
            "allowed_hosts",
            "engine",
            "effort",
        )
        if key in (profile.runner_config or {})
    }
    policy_json = json.dumps(policy, ensure_ascii=False)
    skills_json = json.dumps(profile.skills or [], ensure_ascii=False)

    tools_str = ", ".join(f'"{t}"' for t in tools) if tools else ""

    created_str = (
        profile.created_at.isoformat()
        if hasattr(profile, "created_at") and profile.created_at
        else ""
    )

    return f"""---
agent_id: "{profile.agent_id}"
revision: {profile.revision}
name: "{profile.name}"
role: "{profile.role}"
tools: [{tools_str}]
runner_config: {policy_json}
skills: {skills_json}
created_at: "{created_str}"
---

# Agent Profile: {profile.name}

- **ID:** `{profile.agent_id}`
- **Revision:** `{profile.revision}`
- **Role:** `{profile.role}`
- **Tools:** `{tools_str or 'none'}`

## System Instructions

{profile.system_prompt.strip()}
"""


def parse_agent_profile_markdown(text: str) -> dict[str, Any]:
    """Parse YAML frontmatter and instructions from an agent profile Markdown document."""
    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            raw_meta = parts[1]
            body = parts[2]
            for line in raw_meta.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip().strip('"\'')
                if k in ("runner_config", "skills"):
                    frontmatter[k] = json.loads(v)
                elif k == "tools":
                    frontmatter[k] = re.findall(r'["\']([^"\']+)["\']', v)
                else:
                    frontmatter[k] = v

    instructions = body.strip()
    if "## System Instructions" in instructions:
        instructions = instructions.split("## System Instructions", 1)[1].strip()

    return {
        "agent_id": frontmatter.get("agent_id"),
        "name": frontmatter.get("name"),
        "role": frontmatter.get("role"),
        "tools": frontmatter.get("tools", []),
        "runner_config": frontmatter.get("runner_config", {}),
        "skills": frontmatter.get("skills"),
        "system_prompt": instructions,
    }


class KnowledgeBase:
    """Manages filesystem-backed knowledge trees for projects, components, and agents."""

    def __init__(
        self,
        root_dir: str | os.PathLike[str] | None = None,
        *,
        instance: InstanceContext | None = None,
    ) -> None:
        self.root_dir = (
            Path(root_dir).expanduser().resolve()
            if root_dir
            else DEFAULT_KNOWLEDGE_DIR.resolve()
        )
        self.projects_dir = self.root_dir / "projects"
        self.components_dir = self.root_dir / "components"
        self.agents_dir = self.root_dir / "agents"
        # Per-instance roots are explicit when supplied. The fallback keeps a
        # bare ``KnowledgeBase(root_dir)`` instance-local (siblings of the
        # knowledge tree) instead of writing into the host HOME directory.
        if instance is not None:
            self.state_root = instance.state_root
            self.workspaces_root = instance.workspaces_root
            self.default_workspace = instance.default_workspace
        else:
            self.state_root = self.root_dir.parent / "state"
            self.workspaces_root = self.root_dir.parent / "workspaces"
            self.default_workspace = None

    def git_commit(self, message: str) -> bool:
        """Commit pending changes in the knowledge base repository if git is present."""
        git_dir = self.root_dir / ".git"
        if not git_dir.is_dir():
            return False
        try:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(self.root_dir),
                check=True,
                capture_output=True,
            )
            res = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(self.root_dir),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                logger.info("Knowledge base committed: %s", message)
                return True
            return False
        except Exception as err:
            logger.warning("Failed to commit knowledge base: %s", err)
            return False

    def sync_handbook(self, source: Path | None = None) -> int:
        """Copy the read-only platform handbook into the knowledge base.

        The copy is content-idempotent: an unchanged document is never
        rewritten, so a later handbook revision replaces stale content and a
        repeated startup does not churn the tree. Returns the number of files
        written (``0`` when no handbook source exists).
        """
        handbook_source = source or DEFAULT_HANDBOOK_SOURCE
        if not handbook_source.is_dir():
            return 0
        target_dir = self.root_dir / "handbook"
        target_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for src in sorted(handbook_source.glob("*.md")):
            destination = target_dir / src.name
            content = src.read_text(encoding="utf-8")
            if (
                not destination.is_file()
                or destination.read_text(encoding="utf-8") != content
            ):
                destination.write_text(content, encoding="utf-8")
                written += 1
        return written

    def get_catalog_index(self) -> str:
        """Generate a compact catalog of available projects, components, and agents."""
        lines = ["## Knowledge Base Catalog"]

        lines.append("### Projects:")
        if self.projects_dir.is_dir():
            for p_dir in sorted(self.projects_dir.iterdir()):
                if p_dir.is_dir():
                    summary_file = p_dir / "summary.md"
                    desc = ""
                    if summary_file.is_file():
                        try:
                            first_lines = [
                                l.strip()
                                for l in summary_file.read_text(encoding="utf-8").splitlines()
                                if l.strip() and not l.startswith("#")
                            ]
                            if first_lines:
                                desc = first_lines[0].lstrip("-* ").split(":", 1)[-1].strip()
                        except Exception:
                            pass
                    lines.append(f"- **{p_dir.name}**" + (f": {desc}" if desc else ""))

        lines.append("### Components:")
        if self.components_dir.is_dir():
            for c_dir in sorted(self.components_dir.iterdir()):
                if c_dir.is_dir():
                    rules_file = c_dir / "rules.md"
                    desc = ""
                    if rules_file.is_file():
                        try:
                            first_lines = [
                                l.strip()
                                for l in rules_file.read_text(encoding="utf-8").splitlines()
                                if l.strip() and not l.startswith("#")
                            ]
                            if first_lines:
                                desc = first_lines[0].lstrip("-* ").split(":", 1)[-1].strip()
                        except Exception:
                            pass
                    lines.append(f"- **{c_dir.name}**" + (f": {desc}" if desc else ""))

        lines.append("### Registered Agents:")
        if self.agents_dir.is_dir():
            for a_file in sorted(self.agents_dir.glob("*.md")):
                try:
                    meta = parse_agent_profile_markdown(a_file.read_text(encoding="utf-8"))
                    agent_id = meta.get("agent_id") or a_file.stem
                    role = meta.get("role") or "worker"
                    name = meta.get("name") or agent_id
                    lines.append(f"- **{agent_id}** ({role}): {name}")
                except Exception:
                    lines.append(f"- **{a_file.stem}**")

        return "\n".join(lines)

    def initialize(self, database: Database | None = None) -> None:
        """Create initial directory structure and starter templates if absent."""
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.components_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)

        # 1. Starter project: agent-manager
        am_project_dir = self.projects_dir / "agent-manager"
        am_project_dir.mkdir(parents=True, exist_ok=True)
        am_summary_file = am_project_dir / "summary.md"
        if not am_summary_file.exists():
            am_summary_file.write_text(
                "# Проект: Agent Manager\n\n"
                "- **Назначение**: Персональная агентная система и секретарский интерфейс через Telegram\n"
                "- **Стек**: Python 3.14, FastAPI, SQLite WAL, aiogram 3, Bubblewrap, Pi Runner\n"
                "- **Порт API**: 18767\n"
                "- **Рабочая директория**: instance-managed workspace (configured by RunnerConfig)\n"
                "- **Архитектура**: Единый сервис (FastAPI + QueueDispatcher + TaskExecutionWorker + Telegram Secretary bot)\n",
                encoding="utf-8",
            )

        # 2. Starter component: aiogram
        aiogram_comp_dir = self.components_dir / "aiogram"
        aiogram_comp_dir.mkdir(parents=True, exist_ok=True)
        aiogram_rules_file = aiogram_comp_dir / "rules.md"
        if not aiogram_rules_file.exists():
            aiogram_rules_file.write_text(
                "# Компонент: aiogram 3.x\n\n"
                "- **Версия**: aiogram >= 3.17\n"
                "- **Архитектура**: Router-based организация хендлеров, без глобальных декораторов\n"
                "- **Авторизация**: `AuthMiddleware` на уровне `Dispatcher` (`dp.message.middleware`)\n"
                "- **Очереди**: FIFO-очередь с блокировкой одного активного хода на диалог\n"
                "- **Отправка файлов**: Использование `FSInputFile` для документов\n",
                encoding="utf-8",
            )

        # 3. Starter component: bubblewrap
        bwrap_comp_dir = self.components_dir / "bubblewrap"
        bwrap_comp_dir.mkdir(parents=True, exist_ok=True)
        bwrap_rules_file = bwrap_comp_dir / "rules.md"
        if not bwrap_rules_file.exists():
            bwrap_rules_file.write_text(
                "# Компонент: Bubblewrap Sandbox (pi-runner)\n\n"
                "- **Изоляция**: Запрещено монтировать реальный HOME, соседние репозитории или прод-файлы\n"
                "- **Доступ**: Read-only монтирование рантаймов (Node.js, Python, Pi CLI)\n"
                "- **Воркспейс**: В режим записи монтируется строго целевая папка задачи\n",
                encoding="utf-8",
            )

        # 4. Ensure default agent profiles exist, sync any file changes to DB, then export to files
        if database is not None:
            self.ensure_default_profiles(database)
            self.sync_agents_from_files(database)
            self.sync_agents_from_db(database)

        # 5. Publish the read-only platform handbook for agent discovery
        self.sync_handbook()

        self.git_commit("Initialize knowledge base and agent profiles")

    def ensure_default_profiles(self, database: Database) -> None:
        """Ensure standard agent profiles exist in the database with up-to-date instructions."""
        repo = AgentProfileRepository(database)
        sec_tools = [
            "read", "attach_document", "create_agent", "update_agent", "archive_agent", "list_agents", "get_agent",
            "save_project_summary", "read_knowledge", "list_knowledge", "write_knowledge",
            "delete_knowledge",
            "update_component_rules", "openviking_search", "openviking_read",
            "resolve_workspace", "list_tasks", "get_task", "create_task", "update_task", "get_task_logs", "resume_task_session",
            "list_skills", "install_skill", "create_skill",
            "trigger_review", "trigger_commit_push", "trigger_rollback", "trigger_analysis",
            "subscribe_task", "unsubscribe_task",
            "list_analysis_reports", "get_analysis_report", "list_recommendations",
            "preview_recommendation", "approve_recommendation", "apply_recommendation",
        ]

        secretary_prompt = (
            "# System Secretary & Platform Controller\n\n"
            "You are the System Secretary and Platform Controller. Reply in Russian, clearly and concisely.\n"
            "You coordinate tasks, agents, skills, and the knowledge base.\n\n"
            "### Прямой диалог с пользователем:\n"
            "- Ты ведешь прямой живой диалог с пользователем в Telegram. Если тебе нужны уточнения, параметры или подтверждение — спрашивай пользователя прямо в тексте своего ответа.\n\n"
            "### Голосовые сообщения и контекст речи:\n"
            "- Пользователь регулярно отправляет голосовые сообщения. Аудио транскрибируется автоматически и поступает тебе с префиксом:\n"
            "  `[Голосовое сообщение (автоматическая расшифровка речи, возможны неточности распознавания)]:\n<текст>`\n"
            "- Всегда учитывай возможные погрешности ASR (фонетические ошибки, термины, опечатки в окончаниях).\n"
            "- Если пользователь ссылается на аудио (например: «как я выше в аудио сказал», «послушай голосовое», «я в войсе говорил»),\n"
            "  понимай, что речь идёт о тексте расшифровки выше. Никогда не говори, что аудио не было, и не спорь о формате ввода.\n\n"
            "### Доступные инструменты (Platform Controller Tools):\n"
            "1. **Задачи и сессии:**\n"
            "- `resolve_workspace(workspace, agent_id)`: проверка каталога, исполнителя и режима доступа.\n"
            "- `list_tasks(status, workspace)`: просмотр списка задач с фильтрацией.\n"
            "- `get_task(task_id)`: просмотр карточки задачи.\n"
            "- `create_task(title, description, workspace, agent_id, status, task_type, attachments)`: постановка новой задачи. Поле attachments позволяет передать список входных файлов/артефактов для монтирования в /inbox.\n"
            "- `update_task(task_id, status, title, description, workspace, agent_id, attachments)`: редактирование ТЗ, смена статуса или добавление вложений.\n"
            "- `get_task_logs(task_id)`: просмотр логов сессий и отчётов ревьюера.\n"
            "- `resume_task_session(task_id, message)`: отправить разработчику дополнительные указания и продолжить его сессию.\n"
            "2. **Агенты:**\n"
            "- `list_agents()`: список всех активных агентов в системе.\n"
            "- `get_agent(agent_id)`: получить текущий системный промпт и конфигурацию агента.\n"
            "- `create_agent(agent_id, name, role, tools, skills, system_prompt, runner_config)`: создание нового агента.\n"
            "- `update_agent(agent_id, system_prompt, tools, skills, name, role, runner_config)`: точечная правка существующего агента.\n"
            "- `archive_agent(agent_id)`: архивировать существующего агента (убирает его из активного списка).\n"
            "3. **Скилы:**\n"
            "- `list_skills()`: список всех доступных скилов в системе и их описаний.\n"
            "- `install_skill(name, source)`: установка скила из OpenViking или локальной директории.\n"
            "- `create_skill(name, skill_md, scripts)`: создание нового скила с SKILL.md и скриптами.\n"
            "4. **База Знаний:**\n"
            "- `list_knowledge(category)`: просмотр структуры базы знаний.\n"
            "- `read_knowledge(query)`: чтение документов, описаний проектов и правил компонентов.\n"
            "- `write_knowledge(category, name, content)`: сохранение или обновление документов БЗ.\n"
            "- `delete_knowledge(path)`: удаление одного явно указанного Markdown-файла БЗ после явного подтверждения пользователя.\n"
            "- `save_project_summary(name, summary)`: сохранение/обновление саммари проекта.\n"
            "- `update_component_rules(component, rules)`: сохранение правил архитектурных компонентов.\n"
            "- `attach_document(target)`: отправка файла пользователю в Telegram.\n"
            "5. **OpenViking:**\n"
            "- `openviking_search(query, limit)`: поиск по долгосрочной памяти, документам и ресурсам OpenViking.\n"
            "- `openviking_read(uri)`: чтение полного содержимого по viking:// URI.\n"
            "6. **Триггеры действий:**\n"
            "- `trigger_review(task_id, instructions)`: запуск Code Review для задачи.\n"
            "- `trigger_commit_push(task_id, push, message)`: безопасный коммит и пуш с проверкой CI.\n"
            "- `trigger_rollback(task_id)`: откат изменений задачи.\n"
            "- `trigger_analysis(task_id)`: запуск анализа сессии завершённой задачи.\n\n"
            "### Agent Naming Guidelines for `create_agent`:\n"
            "1. `agent_id`: Use descriptive lowercase identifiers with hyphens (e.g., 'skynet-bot-developer', 'project-scout', 'crypto-pay-tester').\n"
            "2. `name`: Human-readable name (e.g., 'Разработчик Skynet Bot', 'Scout проекта').\n"
            "3. System prefix `system-*`: reserved for platform-wide/infrastructure agents. Only create or modify agents with `system-*` prefix when explicitly requested by the user. For regular tasks, use project-specific identifiers.\n"
        )
        secretary_prompt += "\n\n" + SECRETARY_CONTRACT

        base_state = self.state_root
        base_ws = self.workspaces_root

        try:
            default_cmd, default_runtime = get_default_pi_runtime()
        except Exception:
            # Bootstrap stays usable without a discovered Pi runtime; the
            # profile can still be edited or a runtime added later.
            default_cmd, default_runtime = ["pi"], []

        sec_state = base_state / "secretary"
        sec_ws = base_ws / "secretary"
        sec_state.mkdir(parents=True, exist_ok=True)
        sec_ws.mkdir(parents=True, exist_ok=True)
        sec_cfg = {
            "project": str(sec_ws),
            "state_dir": str(sec_state),
            "pi_command": default_cmd,
            "runtime_paths": default_runtime,
            "tools": sec_tools,
            "network": True,
            "project_read_only": True,
        }

        try:
            secretary = repo.get_current("system-secretary")
            if secretary is None:
                repo.create(
                    agent_id="system-secretary",
                    name="System Secretary",
                    role="secretary",
                    system_prompt=secretary_prompt,
                    runner_config=sec_cfg,
                )
            else:
                updated_cfg = dict(secretary.runner_config or {})
                cfg_changed = False
                if updated_cfg.get("state_dir") != str(sec_state):
                    updated_cfg["state_dir"] = str(sec_state)
                    cfg_changed = True
                if updated_cfg.get("pi_command") != default_cmd:
                    updated_cfg["pi_command"] = default_cmd
                    cfg_changed = True
                if updated_cfg.get("runtime_paths") != default_runtime:
                    updated_cfg["runtime_paths"] = default_runtime
                    cfg_changed = True
                if "project" not in updated_cfg:
                    updated_cfg["project"] = str(sec_ws)
                    cfg_changed = True
                current_tools = list(updated_cfg.get("tools") or [])
                if "ask_clarification" in current_tools:
                    current_tools.remove("ask_clarification")
                    updated_cfg["tools"] = current_tools
                    cfg_changed = True
                if "create_task" in current_tools and "resolve_workspace" not in current_tools:
                    current_tools.append("resolve_workspace")
                    updated_cfg["tools"] = current_tools
                    cfg_changed = True
                if not current_tools:
                    updated_cfg["tools"] = sec_tools
                    cfg_changed = True

                # Never overwrite existing prompt if user customized it
                prompt_to_keep = secretary.system_prompt if secretary.system_prompt.strip() else secretary_prompt
                if cfg_changed or not secretary.system_prompt.strip():
                    repo.update(
                        agent_id="system-secretary",
                        expected_revision=secretary.revision,
                        system_prompt=prompt_to_keep,
                        runner_config=updated_cfg,
                    )
        except Exception as err:
            logger.warning("Could not ensure secretary profile: %s", err)

        dev_state = base_state / "developer"
        dev_state.mkdir(parents=True, exist_ok=True)
        dev_ws = self.default_workspace or (base_ws / "developer")
        dev_ws.mkdir(parents=True, exist_ok=True)
        dev_cfg = {
            "project": str(dev_ws),
            "state_dir": str(dev_state),
            # Tools are listed explicitly. The Runner no longer appends the
            # platform tool surface to every profile.
            "tools": [
                "read", "write", "edit", "bash",
                "ask_clarification", "read_knowledge",
            ],
            "network": True,
        }

        developer_prompt = (
            "# Developer agent\n\n"
            "You develop projects in the configured workspace. Implement requested changes,\n"
            "verify them with tests, follow existing architectural conventions, and provide a clear summary.\n\n"
            "### Ambiguity and Clarifications:\n"
            "Если есть вероятность понять задачу двояко хотя бы на 10% — ты ОБЯЗАН не додумывать за пользователя, а остановиться и вызвать инструмент `ask_clarification(title, description, question)`.\n"
            "- В поле `description` опиши факты, текущие находки, гипотезы и почему возник тупик или неоднозначность (нормальным развернутым текстом на абзац).\n"
            "- В поле `question` сформулируй понятный открытый вопрос пользователю.\n"
            "После вызова инструмента заверши ответ кратким резюме возникшего вопроса.\n\n"
            "### Custom Tools:\n"
            "- `ask_clarification(title, description, question)`: Задать уточняющий вопрос пользователю и перевести задачу в ожидание ответа.\n"
            "- `read_knowledge(query)`: Прочитать дополнительную документацию, правила компонентов или саммори из базы знаний.\n"
        )
        try:
            developer = repo.get_current("agent-manager-developer")
            if developer is None:
                repo.create(
                    agent_id="agent-manager-developer",
                    name="Agent Manager developer",
                    role="developer",
                    system_prompt=developer_prompt,
                    runner_config=dev_cfg,
                )
        except Exception as err:
            logger.warning("Could not ensure developer profile: %s", err)

        analyzer_state = base_state / "analyzer"
        analyzer_ws = base_ws / "analyzer"
        analyzer_state.mkdir(parents=True, exist_ok=True)
        analyzer_ws.mkdir(parents=True, exist_ok=True)
        analyzer_cfg = {
            "project": str(analyzer_ws),
            "state_dir": str(analyzer_state),
            "pi_command": default_cmd,
            "runtime_paths": default_runtime,
            "tools": ["read", "bash"],
            "network": True,
            "project_read_only": True,
        }

        analyzer_prompt = (
            "# System Log & Session Analyzer\n\n"
            "Analyze supplied session logs, execution traces, and evidence. Do not change code or files.\n"
            "Distinguish observed facts from hypotheses.\n"
            "Identify what knowledge was missing, causing excessive searches or errors.\n"
            "Formulate concrete proposals for updating component rules or project summaries in the knowledge base.\n"
        )
        try:
            analyzer = repo.get_current("system-log-analyzer")
            if analyzer is None:
                repo.create(
                    agent_id="system-log-analyzer",
                    name="System Log Analyzer",
                    role="analyzer",
                    system_prompt=analyzer_prompt,
                    runner_config=analyzer_cfg,
                )
            elif not (analyzer.runner_config or {}).get("runtime_paths"):
                updated_cfg = dict(analyzer.runner_config or {})
                updated_cfg["state_dir"] = str(analyzer_state)
                updated_cfg["pi_command"] = default_cmd
                updated_cfg["runtime_paths"] = default_runtime
                if "project" not in updated_cfg:
                    updated_cfg["project"] = str(analyzer_ws)
                repo.update(
                    agent_id="system-log-analyzer",
                    expected_revision=analyzer.revision,
                    runner_config=updated_cfg,
                )
        except Exception as err:
            logger.warning("Could not ensure analyzer profile: %s", err)

        rev_state = base_state / "reviewer"
        rev_ws = base_ws / "reviewer"
        rev_state.mkdir(parents=True, exist_ok=True)
        rev_ws.mkdir(parents=True, exist_ok=True)
        rev_cfg = {
            "project": str(rev_ws),
            "state_dir": str(rev_state),
            "pi_command": default_cmd,
            "runtime_paths": default_runtime,
            "tools": ["read", "bash"],
            "network": True,
            "project_read_only": True,
            # Native AGY reviewer by default; set ``engine`` to ``pi`` or
            # ``codex`` in the profile to switch the review backend.
            "engine": "agy",
            "effort": "high",
        }
        reviewer_prompt = (
            "# System Code Reviewer\n\n"
            "Ты — независимый Code Reviewer. Твоя роль — верифицировать изменения в кодовой базе перед коммитом и отправкой в продакшн.\n"
            "Ты работаешь строго в режиме `project_read_only`. Запрещено изменять файлы в кодовой базе.\n\n"
            "### Принципы проверки:\n"
            "1. Изучи `git diff` и статус незакоммиченных изменений в репозитории.\n"
            "2. Проверь соответствие реализации поставленной задаче и архитектурным соглашениям проекта.\n"
            "3. Проверь наличие багов, краевых случаев, регрессий, корректность обработки ошибок и наличие модульных тестов.\n"
            "4. Выяви критические замечания (P1) и второстепенные предложения (P2/P3).\n\n"
            "### Формат вердикта:\n"
            "- Если обнаружены критические баги, регрессии или отсутствие тестов, опиши каждое замечание четко:\n"
            "  `P1: <файл>:<строка> — суть проблемы и рекомендация по исправлению`\n"
            "  В конце отчёта обязательно укажи отдельной строкой:\n"
            "  `ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ`\n"
            "- Если критических замечаний нет (или только косметические P2/P3):\n"
            "  В конце отчёта обязательно укажи отдельной строкой:\n"
            "  `ВЕРДИКТ: ОДОБРЕНО`\n"
        )
        try:
            reviewer = repo.get_current("system-code-reviewer")
            if reviewer is None:
                repo.create(
                    agent_id="system-code-reviewer",
                    name="System Code Reviewer",
                    role="reviewer",
                    system_prompt=reviewer_prompt,
                    runner_config=rev_cfg,
                )
            else:
                updated_cfg = dict(reviewer.runner_config or {})
                changed = False
                if not updated_cfg.get("runtime_paths"):
                    updated_cfg["state_dir"] = str(rev_state)
                    updated_cfg["pi_command"] = default_cmd
                    updated_cfg["runtime_paths"] = default_runtime
                    if "project" not in updated_cfg:
                        updated_cfg["project"] = str(rev_ws)
                    changed = True
                # Existing profiles without an explicit engine adopt the new
                # default once; a user-selected engine is never overwritten.
                if "engine" not in updated_cfg:
                    updated_cfg["engine"] = "agy"
                    updated_cfg.setdefault("effort", "high")
                    changed = True
                if changed:
                    repo.update(
                        agent_id="system-code-reviewer",
                        expected_revision=reviewer.revision,
                        runner_config=updated_cfg,
                    )
        except Exception as err:
            logger.warning("Could not ensure reviewer profile: %s", err)

    def sync_agents_from_files(self, database: Database) -> list[str]:
        """Import agent profiles from agents/*.md into the database if present."""
        if not self.agents_dir.is_dir():
            return []
        repo = AgentProfileRepository(database)
        imported: list[str] = []
        for file in self.agents_dir.glob("*.md"):
            try:
                text = file.read_text(encoding="utf-8")
                parsed = parse_agent_profile_markdown(text)
                agent_id = parsed.get("agent_id") or file.stem
                name = parsed.get("name") or agent_id
                role = parsed.get("role") or "worker"
                tools = parsed.get("tools") or []
                prompt = parsed.get("system_prompt") or ""
                if not prompt.strip():
                    continue

                current = repo.get_current(agent_id)
                if current is None:
                    repo.create(
                        agent_id=agent_id,
                        name=name,
                        role=role,
                        system_prompt=prompt,
                        runner_config={"tools": tools, **parsed.get("runner_config", {})},
                        skills=parsed.get("skills") or [],
                    )
                    imported.append(agent_id)
                else:
                    current_tools = (
                        list(current.runner_config.get("tools") or [])
                        if isinstance(current.runner_config, dict)
                        else []
                    )
                    if (
                        current.system_prompt.strip() != prompt.strip()
                        or current_tools != tools
                        or any(current.runner_config.get(key) != value for key, value in parsed.get("runner_config", {}).items())
                        or (parsed.get("skills") is not None and current.skills != parsed["skills"])
                    ):
                        updated_cfg = dict(current.runner_config or {})
                        updated_cfg.update(parsed.get("runner_config", {}))
                        updated_cfg["tools"] = tools
                        updated_cfg.pop("provider", None)
                        updated_cfg.pop("model", None)
                        repo.update(
                            agent_id=agent_id,
                            expected_revision=current.revision,
                            name=name,
                            role=role,
                            system_prompt=prompt,
                            runner_config=updated_cfg,
                            skills=parsed["skills"] if parsed.get("skills") is not None else current.skills,
                        )
                        imported.append(agent_id)
            except Exception as e:
                logger.warning("Failed to sync agent profile from %s: %s", file, e)

        # Check for non-system agents deleted from disk and archive them in database
        try:
            git_dir = self.root_dir / ".git"
            deleted_agent_ids: set[str] = set()
            if git_dir.is_dir():
                res = subprocess.run(
                    ["git", "ls-files", "agents/"],
                    cwd=str(self.root_dir),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0:
                    for line in res.stdout.splitlines():
                        line = line.strip()
                        if line.startswith("agents/") and line.endswith(".md"):
                            stem = Path(line).stem
                            if not (self.root_dir / line).is_file():
                                deleted_agent_ids.add(stem)

            for a_id in deleted_agent_ids:
                if a_id in CORE_SYSTEM_AGENTS:
                    continue
                prof = repo.get_current(a_id)
                if prof is not None and not (
                    isinstance(prof.runner_config, dict)
                    and prof.runner_config.get("archived") is True
                ):
                    repo.archive(a_id, expected_revision=prof.revision)
                    logger.info("Archived agent %s because file was deleted from disk", a_id)
                    imported.append(f"archived:{a_id}")
        except Exception as e:
            logger.warning("Failed checking for removed agent files: %s", e)

        return imported

    def sync_agent_from_file(self, agent_id: str, database: Database) -> bool:
        """Check if agents/<agent_id>.md was modified and update database revision if so."""
        agent_file = self.agents_dir / f"{agent_id}.md"
        repo = AgentProfileRepository(database)
        if not agent_file.is_file():
            # If non-core agent file was deleted, archive it
            if agent_id not in CORE_SYSTEM_AGENTS:
                git_dir = self.root_dir / ".git"
                was_tracked = True
                if git_dir.is_dir():
                    res = subprocess.run(
                        ["git", "ls-files", f"agents/{agent_id}.md"],
                        cwd=str(self.root_dir),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    was_tracked = res.returncode == 0 and bool(res.stdout.strip())
                if was_tracked:
                    current = repo.get_current(agent_id)
                    if current is not None and not (
                        isinstance(current.runner_config, dict)
                        and current.runner_config.get("archived") is True
                    ):
                        repo.archive(agent_id, expected_revision=current.revision)
                        logger.info("Archived agent %s because file %s was deleted", agent_id, agent_file)
                        return True
            return False
        try:
            text = agent_file.read_text(encoding="utf-8")
            parsed = parse_agent_profile_markdown(text)
            prompt = (parsed.get("system_prompt") or "").strip()
            tools = parsed.get("tools") or []
            if not prompt:
                return False

            current = repo.get_current(agent_id)
            if current is None:
                repo.create(
                    agent_id=agent_id,
                    name=parsed.get("name") or agent_id,
                    role=parsed.get("role") or "worker",
                    system_prompt=prompt,
                    runner_config={"tools": tools, **parsed.get("runner_config", {})},
                        skills=parsed.get("skills") or [],
                )
                logger.info("Hot-reloaded new agent %s from %s", agent_id, agent_file)
                return True

            current_tools = (
                list(current.runner_config.get("tools") or [])
                if isinstance(current.runner_config, dict)
                else []
            )
            is_archived = (
                isinstance(current.runner_config, dict)
                and current.runner_config.get("archived") is True
            )
            policy_changed = any(current.runner_config.get(key) != value for key, value in parsed.get("runner_config", {}).items())
            skills_changed = parsed.get("skills") is not None and current.skills != parsed["skills"]
            if current.system_prompt.strip() != prompt or current_tools != tools or is_archived or policy_changed or skills_changed:
                updated_cfg = dict(current.runner_config or {})
                updated_cfg.update(parsed.get("runner_config", {}))
                updated_cfg["tools"] = tools
                updated_cfg.pop("provider", None)
                updated_cfg.pop("model", None)
                if is_archived:
                    updated_cfg.pop("archived", None)
                repo.update(
                    agent_id=agent_id,
                    expected_revision=current.revision,
                    name=parsed.get("name") or current.name,
                    role=parsed.get("role") or current.role,
                    system_prompt=prompt,
                    runner_config=updated_cfg,
                    skills=parsed["skills"] if parsed.get("skills") is not None else current.skills,
                )
                logger.info("Hot-reloaded agent %s revision from %s", agent_id, agent_file)
                return True
        except Exception as e:
            logger.warning("Failed to hot-reload agent %s from %s: %s", agent_id, agent_file, e)
        return False

    def sync_agents_from_db(self, database: Database) -> list[str]:
        """Export current revisions of active registered agent profiles to agents/*.md."""
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        repo = AgentProfileRepository(database)
        exported: list[str] = []
        for profile in repo.list_current(include_archived=False):
            md_content = format_agent_profile_markdown(profile)
            target = self.agents_dir / f"{profile.agent_id}.md"
            target.write_text(md_content, encoding="utf-8")
            exported.append(profile.agent_id)
        if exported:
            self.git_commit("Export agent profiles from database")
        return exported

    def archive_agent(self, agent_id: str, database: Database) -> bool:
        """Archive an agent in DB and remove its markdown file from disk."""
        if agent_id in CORE_SYSTEM_AGENTS:
            raise ValueError(f"Core system agent '{agent_id}' cannot be archived")
        repo = AgentProfileRepository(database)
        current = repo.get_current(agent_id)
        if current is None:
            return False
        repo.archive(agent_id, expected_revision=current.revision)
        target = self.agents_dir / f"{agent_id}.md"
        if target.is_file():
            target.unlink()
            self.git_commit(f"Archive agent profile: {agent_id}")
        return True

    def save_agent_profile(self, profile: AgentProfile) -> Path:
        """Write one agent profile to agents/<agent_id>.md."""
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        target = self.agents_dir / f"{profile.agent_id}.md"
        md_content = format_agent_profile_markdown(profile)
        target.write_text(md_content, encoding="utf-8")
        self.git_commit(f"Save agent profile: {profile.agent_id}")
        return target

    def create_or_update_project_summary(
        self, project_name: str, content: str
    ) -> Path:
        """Create or update a project's summary.md in the knowledge base and commit."""
        summary_file = contained_target_path(
            self.projects_dir, project_name, "summary.md"
        )
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        summary_file.write_text(content.strip() + "\n", encoding="utf-8")
        self.git_commit(f"Update summary for project: {project_name}")
        return summary_file

    def update_component_rules(self, component: str, rules: str) -> Path:
        """Create or update a component's rules.md in the knowledge base and commit."""
        rules_file = contained_target_path(
            self.components_dir, component, "rules.md"
        )
        rules_file.parent.mkdir(parents=True, exist_ok=True)
        rules_file.write_text(rules.strip() + "\n", encoding="utf-8")
        self.git_commit(f"Update rules for component: {component}")
        return rules_file

    def resolve_file(self, query_or_path: str) -> Path | None:
        """Resolve a query or file path to an existing file in knowledge base.

        Supports:
        - Absolute path if existing
        - Relative path from root_dir: e.g. "agents/agent-manager-developer.md"
        - Shorthand by name: e.g. "agent-manager-developer" -> "agents/agent-manager-developer.md"
        - Project summary: e.g. "agent-manager" -> "projects/agent-manager/summary.md"
        - Component rules: e.g. "aiogram" -> "components/aiogram/rules.md"
        """
        stripped = query_or_path.strip().strip("'\"")

        # 1. Exact path
        direct = Path(stripped).expanduser()
        if direct.is_file():
            return direct.resolve()

        # 2. Relative to root_dir
        rel = (self.root_dir / stripped).resolve()
        if rel.is_file():
            return rel

        # If has .md extension
        if not stripped.endswith(".md"):
            with_md = (self.root_dir / f"{stripped}.md").resolve()
            if with_md.is_file():
                return with_md

        # 3. Check in agents/
        agent_file = self.agents_dir / f"{stripped}.md"
        if agent_file.is_file():
            return agent_file

        # 4. Check in projects/<name>/summary.md
        project_file = self.projects_dir / stripped / "summary.md"
        if project_file.is_file():
            return project_file

        # 5. Check in components/<name>/rules.md
        comp_file = self.components_dir / stripped / "rules.md"
        if comp_file.is_file():
            return comp_file

        return None

    def list_documents(self, category: str | None = None) -> list[str]:
        """List relative paths of markdown documents in knowledge base."""
        target_dir = (self.root_dir / category).resolve() if category else self.root_dir
        if not target_dir.is_relative_to(self.root_dir) or not target_dir.is_dir():
            return []
        items: list[str] = []
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in sorted(files):
                if f.startswith(".") or not f.endswith(".md"):
                    continue
                p = Path(root) / f
                try:
                    rel = p.relative_to(self.root_dir)
                    items.append(str(rel))
                except ValueError:
                    pass
        return sorted(items)

    def read_document(self, relative_path: str) -> str | None:
        """Read a document from knowledge base by relative path or query."""
        resolved = self.resolve_file(relative_path)
        if resolved is not None and resolved.is_file():
            try:
                if not resolved.is_relative_to(self.root_dir):
                    return None
                return resolved.read_text(encoding="utf-8")
            except Exception:
                return None
        candidate = (self.root_dir / relative_path.strip().lstrip("/")).resolve()
        if candidate.is_relative_to(self.root_dir) and candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        return None

    def write_document(
        self, relative_path: str, content: str, commit_message: str | None = None
    ) -> Path:
        """Write a document to knowledge base, creating directories, and commit."""
        clean_path = relative_path.strip().lstrip("/")
        if not clean_path.endswith(".md"):
            clean_path = f"{clean_path}.md"
        candidate = (self.root_dir / clean_path).resolve()
        if not candidate.is_relative_to(self.root_dir):
            raise KnowledgePathError(f"Path escapes knowledge root: {relative_path}")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content.strip() + "\n", encoding="utf-8")
        msg = commit_message or f"Update knowledge document: {clean_path}"
        self.git_commit(msg)
        return candidate

    def delete_document(
        self, relative_path: str, commit_message: str | None = None
    ) -> Path:
        """Delete one explicitly named Markdown document from the knowledge base.

        Only a single existing ``.md`` file inside :attr:`root_dir` is
        accepted. Absolute paths, empty or ``.``/``..`` segments, wildcard
        characters, directories and symlinks are rejected, as is any path
        under ``agents/`` because profiles are managed by the profile API.
        The exact file is unlinked; no wildcard or recursive deletion exists.
        When the knowledge tree is a git repository the removal is committed
        with the same helper used by document writes.
        """
        raw_path = relative_path.strip() if isinstance(relative_path, str) else ""
        if not raw_path:
            raise KnowledgePathError("Knowledge document path is required")
        if "\x00" in raw_path or "\\" in raw_path:
            raise KnowledgePathError(
                f"Invalid knowledge document path: {relative_path!r}"
            )
        if raw_path.startswith("/") or Path(raw_path).is_absolute():
            raise KnowledgePathError(
                f"Knowledge document path must be relative: {relative_path!r}"
            )

        parts = raw_path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise KnowledgePathError(
                f"Knowledge document path must name one exact file: {relative_path!r}"
            )
        if any(any(char in part for char in "*?[]") for part in parts):
            raise KnowledgePathError(
                "Wildcard knowledge deletion is not supported; name one exact file"
            )
        if not parts[-1].endswith(".md") or parts[-1] == ".md":
            raise KnowledgePathError(
                "Only an explicitly named Markdown document can be deleted"
            )
        if parts[0] == "agents":
            raise KnowledgeProtectedPathError(
                "Agent profile files are managed by the profile API and cannot "
                "be deleted as knowledge documents"
            )

        # Reject a symlink at any component so a path cannot be redirected
        # into another tree. ``resolve`` below additionally catches a symlink
        # race and verifies the real target stays inside the knowledge root.
        unresolved = self.root_dir
        for part in parts:
            unresolved = unresolved / part
            if unresolved.is_symlink():
                raise KnowledgePathError(
                    f"Knowledge document path must not use symlinks: {relative_path!r}"
                )

        candidate = unresolved.resolve()
        if not candidate.is_relative_to(self.root_dir):
            raise KnowledgePathError(
                f"Knowledge document path escapes the knowledge root: {relative_path!r}"
            )
        if candidate.is_relative_to(self.agents_dir.resolve()):
            raise KnowledgeProtectedPathError(
                "Agent profile files are managed by the profile API and cannot "
                "be deleted as knowledge documents"
            )
        if not candidate.exists():
            raise KnowledgeDocumentNotFoundError(
                f"Knowledge document not found: {relative_path}"
            )
        if not candidate.is_file():
            raise KnowledgePathError(
                "Only an explicitly named Markdown file can be deleted, not a "
                "directory or special file"
            )

        candidate.unlink()
        clean_path = "/".join(parts)
        self.git_commit(
            commit_message or f"Delete knowledge document: {clean_path}"
        )
        return candidate

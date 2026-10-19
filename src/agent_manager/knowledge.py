"""Knowledge base management for projects, components, and agent profiles."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

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


import shutil


def get_default_pi_runtime() -> tuple[list[str], list[str]]:
    """Return default (pi_command, runtime_paths) for host environment."""
    node_exe = shutil.which("node") or "/home/itolstov/.nvm/versions/node/v24.19.0/bin/node"
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
                if k == "tools":
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
        "system_prompt": instructions,
    }


class KnowledgeBase:
    """Manages filesystem-backed knowledge trees for projects, components, and agents."""

    def __init__(self, root_dir: str | os.PathLike[str] | None = None) -> None:
        self.root_dir = (
            Path(root_dir).expanduser().resolve()
            if root_dir
            else DEFAULT_KNOWLEDGE_DIR.resolve()
        )
        self.projects_dir = self.root_dir / "projects"
        self.components_dir = self.root_dir / "components"
        self.agents_dir = self.root_dir / "agents"

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
                "- **Рабочая директория**: `/home/itolstov/Projects/mtl/AgentManager/agent-manager`\n"
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

        self.git_commit("Initialize knowledge base and agent profiles")

    def ensure_default_profiles(self, database: Database) -> None:
        """Ensure standard agent profiles exist in the database with up-to-date instructions."""
        repo = AgentProfileRepository(database)
        sec_tools = [
            "read", "attach_document", "create_agent", "update_agent", "archive_agent", "list_agents", "get_agent",
            "save_project_summary", "read_knowledge", "list_knowledge", "write_knowledge",
            "update_component_rules", "openviking_search", "openviking_read",
            "list_tasks", "get_task", "create_task", "update_task", "get_task_logs", "resume_task_session",
            "list_skills", "install_skill", "create_skill",
            "trigger_review", "trigger_commit_push", "trigger_rollback", "trigger_analysis"
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
            "- `list_tasks(status, workspace)`: просмотр списка задач с фильтрацией.\n"
            "- `get_task(task_id)`: просмотр карточки задачи.\n"
            "- `create_task(title, description, workspace, status, task_type)`: постановка новой задачи.\n"
            "- `update_task(task_id, status, title, description)`: редактирование ТЗ или смена статуса.\n"
            "- `get_task_logs(task_id)`: просмотр логов сессий и отчётов ревьюера.\n"
            "- `resume_task_session(task_id, message)`: отправить разработчику дополнительные указания и продолжить его сессию.\n"
            "2. **Агенты:**\n"
            "- `list_agents()`: список всех активных агентов в системе.\n"
            "- `get_agent(agent_id)`: получить текущий системный промпт и конфигурацию агента.\n"
            "- `create_agent(agent_id, name, role, tools, skills, system_prompt)`: создание нового агента.\n"
            "- `update_agent(agent_id, system_prompt, tools, skills, name, role)`: точечная правка существующего агента.\n"
            "- `archive_agent(agent_id)`: архивировать существующего агента (убирает его из активного списка).\n"
            "3. **Скилы:**\n"
            "- `list_skills()`: список всех доступных скилов в системе и их описаний.\n"
            "- `install_skill(name, source)`: установка скила из OpenViking или локальной директории.\n"
            "- `create_skill(name, skill_md, scripts)`: создание нового скила с SKILL.md и скриптами.\n"
            "4. **База Знаний:**\n"
            "- `list_knowledge(category)`: просмотр структуры базы знаний.\n"
            "- `read_knowledge(query)`: чтение документов, описаний проектов и правил компонентов.\n"
            "- `write_knowledge(category, name, content)`: сохранение или обновление документов БЗ.\n"
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
        base_state = Path.home() / ".local/share/agent-manager/state"
        base_ws = Path.home() / ".local/share/agent-manager/workspaces"

        default_cmd, default_runtime = get_default_pi_runtime()

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
        dev_cfg = {
            "project": "/home/itolstov/Projects/mtl/AgentManager/agent-manager",
            "state_dir": str(dev_state),
            "tools": ["read", "write", "edit", "bash"],
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
            elif not (reviewer.runner_config or {}).get("runtime_paths"):
                updated_cfg = dict(reviewer.runner_config or {})
                updated_cfg["state_dir"] = str(rev_state)
                updated_cfg["pi_command"] = default_cmd
                updated_cfg["runtime_paths"] = default_runtime
                if "project" not in updated_cfg:
                    updated_cfg["project"] = str(rev_ws)
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
                        runner_config={"tools": tools},
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
                    ):
                        updated_cfg = dict(current.runner_config or {})
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
                    runner_config={"tools": tools},
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
            if current.system_prompt.strip() != prompt or current_tools != tools or is_archived:
                updated_cfg = dict(current.runner_config or {})
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
        project_dir = self.projects_dir / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        summary_file = project_dir / "summary.md"
        summary_file.write_text(content.strip() + "\n", encoding="utf-8")
        self.git_commit(f"Update summary for project: {project_name}")
        return summary_file

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

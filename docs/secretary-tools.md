# Инструменты Секретаря (Platform Controller & Secretary Tools)

Секретарь (`system-secretary`) в Agent Manager выполняет роль главного диспетчера и контроллера всей агентной платформы. Работая через Telegram, он имеет доступ к набору инструментов (доступных через policy `pi-runner` и REST API Agent Manager).

---

## 1. Задачи и сессии разработки

### `list_tasks(status, workspace)`
- **Назначение**: Просмотр задач в очереди с фильтрацией.
- **Параметры**:
  - `status` (string, опц.): `incoming`, `in_progress`, `ready_to_commit`, `done`, `failed`.
  - `workspace` (string, опц.): имя воркспейса проекта (например `agent-manager`).

### `get_task(task_id)`
- **Назначение**: Получение полной карточки задачи, статуса, метаданных и отчёта.

### `create_task(title, description, workspace, status, task_type)`
- **Назначение**: Постановка новой задачи разработчику.
- **Параметры**:
  - `title` (string): заголовок задачи.
  - `description` (string, опц.): подробное ТЗ.
  - `workspace` (string, опц.): целевой репозиторий.
  - `status` (string, по умолчанию `incoming`): статус.
  - `task_type` (string, по умолчанию `coding`): `coding`, `devops`, `research`.

### `update_task(task_id, status, title, description)`
- **Назначение**: Редактирование ТЗ или смена статуса задачи.

### `get_task_logs(task_id)`
- **Назначение**: Просмотр логов раундов Code Review (`round_1.json`, `review_round_1.md`) и отчетов разработчика.

### `resume_task_session(task_id, message)`
- **Назначение**: Отправка дополнительных инструкций агенту-разработчику и продолжение его существующей сессии.

---

## 2. Управление Агентами (Agent Profiles)

### `list_agents()`
- **Назначение**: Просмотр списка всех зарегистрированных агентов в платформе.

### `get_agent(agent_id)`
- **Назначение**: Получение текущей конфигурации, инструментов, скилов и системного промпта агента.

### `create_agent(agent_id, name, role, tools, skills, system_prompt)`
- **Назначение**: Регистрация нового агента проекта.
- **Правило именования**: строго `<project>-developer` в нижнем регистре (префикс `system-*` зарезервирован для платформенных агентов).

### `update_agent(agent_id, system_prompt, tools, skills, name, role)`
- **Назначение**: Обновление системного промпта, набора инструментов или скилов агента (создает новую ревизию с optimistic lock).

---

## 3. Управление Скилами (Skills)

### `list_skills()`
- **Назначение**: Список всех установленных скилов в `~/.local/share/agent-manager/skills` с описанием из `SKILL.md`.

### `install_skill(name, source)`
- **Назначение**: Установка скила из директории (например из каталога OpenViking или локальной папки).

### `create_skill(name, skill_md, scripts)`
- **Назначение**: Создание нового скила «на лету» с генерацией `SKILL.md` и вспомогательных скриптов.

---

## 4. База Знаний (Knowledge Base)

### `list_knowledge(category)`
- **Назначение**: Просмотр дерева базы знаний (`projects`, `components`, `docs`).

### `read_knowledge(query)`
- **Назначение**: Полнотекстовый поиск и чтение файлов документации, правил компонентов или саммари проектов.

### `write_knowledge(category, name, content)`
- **Назначение**: Сохранение произвольного документа в базу знаний с автоматическим git-коммитом в репозиторий знаний.

### `save_project_summary(name, summary)`
- **Назначение**: Обновление карточки проекта (`knowledge/projects/<name>/summary.md`).

### `update_component_rules(component, rules)`
- **Назначение**: Обновление архитектурных правил компонента (`knowledge/components/<component>/rules.md`).

### `attach_document(target)`
- **Назначение**: Отправка документа или файла логов напрямую пользователю в Telegram.

---

## 5. Долгосрочная память OpenViking

### `openviking_search(query, limit)`
- **Назначение**: Семантический поиск по долговременной памяти, внешним документам и коду через локальный сервис OpenViking (`http://127.0.0.1:1933`).

### `openviking_read(uri)`
- **Назначение**: Чтение полного текста ресурса по URI вида `viking://...`.

---

## 6. Триггеры и Git Engine

### `trigger_review(task_id, instructions)`
- **Назначение**: Принудительный перезапуск раунда Code Review независимым ревьюером (`system-code-reviewer`).

### `trigger_commit_push(task_id, push, message)`
- **Назначение**: Безопасный коммит измененных файлов через `GitEngine` с проверкой GitHub Actions CI (`gh run watch`).

### `trigger_rollback(task_id)`
- **Назначение**: Безопасный откат незакоммиченных изменений (`git restore --staged --worktree`) и удаление созданных файлов.

### `trigger_analysis(task_id)`
- **Назначение**: Запуск агента `system-log-analyzer` для анализа сессии завершенной задачи и формулирования предложений в Базу Знаний.

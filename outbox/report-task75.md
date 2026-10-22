# Отчёт: handbook kuzya — пробелы по статусам/git и полное покрытие API

Дата: 2026-10-22. Область: только документация. Код, миграции, API,
тесты-исходники не изменялись; сервис не перезапускался; commit/push не
выполнялись; прод-данные и токены не читались.

## 0. Краткий итог

- Закрыты 4 подтверждённых пробела части 1: `archived`, resume терминальных
  задач, успешный `git.commit_push` без изменений, read-only рецепт сверки
  git-состояния (+ различие `metadata.changed_files` /
  `provenance.changed_files`).
- Проведён аудит всей REST-поверхности: **45 method+path** (статически, по
  `src/kuzya/api/app.py`; OpenAPI сгенерировать не удалось — см. раздел 2).
- До аудита в таблице REST не было 8 маршрутов; 5 из них не были описаны
  нигде в хендбуке, ещё 3 существовали только как фрагменты предметных
  разделов. У остальных 40 были строка в таблице и/или фрагмент, без
  параметров, тел, полей ответа и полного набора кодов.
- После аудита все 45 маршрутов описаны в `docs/handbook/tools-and-api.md`
  (параметры path/query, тела со всеми полями и обязательностью, поля ответа,
  успешные/ошибочные коды, идемпотентность, связанные состояния).
- Инвентарь инструментов приведён в соответствие с кодом: **38**, а не 37;
  добавлена строка `call_api`, которой не было в таблице.
- `docs/api.md` содержал несуществующий маршрут
  `GET /conversations/{id}/turns/active` и неверное поле идемпотентности
  `idempotency_key`; исправлено.
- Открытая находка для отдельной задачи (P1): `/mcp` смонтирован в приложении,
  но отсутствует в `capabilities.py` и вне OpenAPI-аудита.

## 1. Метод

1. `.belief_map.sexp` в рабочей директории отсутствует. Границы инспекции
   выбраны по `AGENTS.md`: `src/kuzya/api/app.py`, `src/kuzya/api/schemas.py`,
   `src/kuzya/capabilities.py`, `src/kuzya/permissions.py`,
   `src/pi_runner/policy.mjs`, `src/kuzya/storage/repository.py`,
   `src/kuzya/git_engine.py`, `src/kuzya/operation_handlers.py`,
   `src/kuzya/task_subscriptions.py`, `src/kuzya/analysis.py` и
   `docs/handbook/*.md`.
2. Перечень маршрутов получен статическим разбором декораторов
   `@app.<method>("<path>")` (включая многострочные) и сверен с
   `_endpoint(...)` / генератором subscriptions в `capabilities.py`.
3. Перечень инструментов получен разбором `pi.registerTool({ name: ... })`
   и цикла `for (const [name, method] of [["subscribe_task", ...],
   ["unsubscribe_task", ...]])`, затем сверен с таблицей
   `capability-inventory.md`.
4. Поля запросов/ответов сверены с Pydantic-моделями
   `src/kuzya/api/schemas.py` и docstring'ами маршрутов.
5. Права сверены с `_ROUTE_RULES` и `ACTIONS_FOR_TOOLS`
   (`src/kuzya/permissions.py`).
6. Факты части 1 сверены с `TaskRepository.update` /
   `claim_in_progress` / `get_status_counts`, `GitEngine.commit_local` /
   `commit_and_push`, `git_commit_push_handler`, `resume_task_session`
   в `policy.mjs` и с данными наблюдавшейся сессии из задания.

## 2. Окружение и блокеры (без выдуманных результатов)

- `PYTHONPATH=src .venv/bin/python -m pytest tests/test_capabilities.py -q`
  **не запускался**: интерпретатора нет.
  Фактический вывод: `/bin/bash: line 1: .venv/bin/python: No such file or directory`.
- Системный `python3` не содержит зависимостей проекта:
  `python3 -c "import fastapi"` → `ModuleNotFoundError: No module named 'fastapi'`.
  Поэтому генерация OpenAPI командой из `tools-and-api.md` недоступна.
- Node есть (`node v24.19.0`), но реальные инструменты не перечислить:
  `node tests/policy_tool_names.mjs src/pi_runner/policy.mjs` →
  `Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'typebox' imported from /workspace/src/pi_runner/policy.mjs`.
- `markdownlint` / `markdownlint-cli2` не установлены; `npx` есть, но
  установка пакета не выполнялась (побочный эффект вне задачи).
- Компенсация: статический разбор + воспроизводимые stdlib-скрипты сравнения
  (раздел 6). Это не заменяет runtime-проверку OpenAPI; остаточный риск
  зафиксирован в разделе 7.

## 3. Матрица покрытия ДО и ПОСЛЕ

Обозначения: **П** — описано полностью (path/query, тело, поля ответа,
коды, идемпотентность/состояния); **Ч** — частично (маршрут упомянут, но
нет полного контракта); **—** — отсутствует.

| # | METHOD / path | ДО: документ/раздел | ДО | ПОСЛЕ: документ/раздел | ПОСЛЕ |
|---|---|---|---|---|---|
| 1 | `GET /health` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Health and workspace → `GET /health` | П |
| 2 | `GET /workspaces/resolve` | tools-and-api.md → REST endpoints; workspaces-and-routing.md → Workspace resolution | Ч | tools-and-api.md → Health and workspace → `GET /workspaces/resolve` | П |
| 3 | `POST /tasks` | tools-and-api.md → REST endpoints; task-lifecycle.md → Creating and editing | Ч | tools-and-api.md → Tasks → `POST /tasks` | П |
| 4 | `GET /tasks` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Tasks → `GET /tasks` | П |
| 5 | `GET /tasks/counts` | tools-and-api.md → REST endpoints; task-lifecycle.md → Creating and editing | Ч | tools-and-api.md → Tasks → `GET /tasks/counts` | П |
| 6 | `GET /tasks/{task_id}` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Tasks → `GET /tasks/{task_id}` | П |
| 7 | `PATCH /tasks/{task_id}` | tools-and-api.md → REST endpoints; task-lifecycle.md → Creating and editing | Ч | tools-and-api.md → Tasks → `PATCH /tasks/{task_id}` | П |
| 8 | `GET /tasks/{task_id}/artifacts` | — | — | tools-and-api.md → Tasks → artifacts | П |
| 9 | `GET /tasks/{task_id}/artifacts/{artifact_name}` | — | — | tools-and-api.md → Tasks → artifact download | П |
| 10 | `GET /tasks/{task_id}/session` | — | — | tools-and-api.md → Tasks → session | П |
| 11 | `POST /tasks/{task_id}/attachments` | tools-and-api.md → REST endpoints; knowledge-and-integrations.md → Files and documents | Ч | tools-and-api.md → Tasks → attachments | П |
| 12 | `POST /tasks/{task_id}/subscriptions` | task-lifecycle.md → Completion subscriptions | Ч | tools-and-api.md → Tasks → subscriptions | П |
| 13 | `DELETE /tasks/{task_id}/subscriptions` | task-lifecycle.md → Completion subscriptions | Ч | tools-and-api.md → Tasks → subscriptions | П |
| 14 | `POST /imports/tasks` | tools-and-api.md → REST endpoints; task-lifecycle.md → Creating and editing | Ч | tools-and-api.md → Tasks → `POST /imports/tasks` | П |
| 15 | `POST /ask` | — | — | tools-and-api.md → Ask → `POST /ask` | П |
| 16 | `POST /conversations/ask` | — | — | tools-and-api.md → Ask → alias | П |
| 17 | `POST /conversations` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Conversations → create | П |
| 18 | `GET /conversations` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Conversations → list | П |
| 19 | `GET /conversations/{conversation_id}` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Conversations → read | П |
| 20 | `POST /conversations/{conversation_id}/messages` | tools-and-api.md → REST endpoints; objects.md → Conversation, Message and Turn | Ч | tools-and-api.md → Conversations → append message | П |
| 21 | `GET /conversations/{conversation_id}/messages` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Conversations → list messages | П |
| 22 | `GET /telegram/links/{chat_id}/{telegram_message_id}` | knowledge-and-integrations.md → Dialog context, UUID and Telegram delivery | Ч | tools-and-api.md → Conversations → Telegram links | П |
| 23 | `POST /conversations/{conversation_id}/turns` | tools-and-api.md → REST endpoints; objects.md → Conversation, Message and Turn; task-lifecycle.md → Turns | Ч | tools-and-api.md → Turns → create | П |
| 24 | `GET /turns/{turn_id}` | tools-and-api.md → REST endpoints; objects.md → Conversation, Message and Turn | Ч | tools-and-api.md → Turns → read | П |
| 25 | `POST /operations` | tools-and-api.md → REST endpoints/Idempotency; approvals-and-revisions.md → Operation idempotency and approval | Ч | tools-and-api.md → Operations → submit | П |
| 26 | `GET /operations` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Operations → list | П |
| 27 | `GET /operations/{operation_id}` | tools-and-api.md → REST endpoints; errors-security-isolation.md → Operation failures | Ч | tools-and-api.md → Operations → read | П |
| 28 | `GET /tasks/{task_id}/analysis` | tools-and-api.md → REST endpoints; analysis-and-recommendations.md → Report and recommendations | Ч | tools-and-api.md → Analysis → task reports | П |
| 29 | `GET /analysis/{report_id}` | tools-and-api.md → REST endpoints; analysis-and-recommendations.md → Report and recommendations | Ч | tools-and-api.md → Analysis → report | П |
| 30 | `GET /analysis/{report_id}/recommendations` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Analysis → report recommendations | П |
| 31 | `GET /recommendations` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Analysis → list recommendations | П |
| 32 | `GET /recommendations/{recommendation_id}` | tools-and-api.md → REST endpoints; analysis-and-recommendations.md → Approval and apply safety | Ч | tools-and-api.md → Analysis → preview | П |
| 33 | `POST /recommendations/{recommendation_id}/approve` | tools-and-api.md → REST endpoints; approvals-and-revisions.md → Recommendation approvals | Ч | tools-and-api.md → Analysis → approve | П |
| 34 | `POST /recommendations/{recommendation_id}/apply` | tools-and-api.md → REST endpoints; approvals-and-revisions.md → Recommendation approvals | Ч | tools-and-api.md → Analysis → apply | П |
| 35 | `POST /agents` | tools-and-api.md → REST endpoints; profiles-runtime-skills.md → Identity and revisions | Ч | tools-and-api.md → Agents → create | П |
| 36 | `GET /agents` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Agents → list | П |
| 37 | `GET /agents/{agent_id}` | tools-and-api.md → REST endpoints; profiles-runtime-skills.md → Identity and revisions | Ч | tools-and-api.md → Agents → read | П |
| 38 | `GET /agents/{agent_id}/revisions` | tools-and-api.md → REST endpoints; profiles-runtime-skills.md → Identity and revisions | Ч | tools-and-api.md → Agents → revisions | П |
| 39 | `GET /agents/{agent_id}/revisions/{revision}` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Agents → revision | П |
| 40 | `PATCH /agents/{agent_id}` | tools-and-api.md → REST endpoints; approvals-and-revisions.md → Immutable profile revisions | Ч | tools-and-api.md → Agents → patch | П |
| 41 | `DELETE /agents/{agent_id}` | tools-and-api.md → REST endpoints; profiles-runtime-skills.md → Identity and revisions | Ч | tools-and-api.md → Agents → archive | П |
| 42 | `GET /knowledge` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Knowledge → list | П |
| 43 | `GET /knowledge/{path}` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Knowledge → read | П |
| 44 | `PUT /knowledge/{path}` | tools-and-api.md → REST endpoints | Ч | tools-and-api.md → Knowledge → write | П |
| 45 | `DELETE /knowledge/{path}` | tools-and-api.md → REST endpoints; knowledge-and-integrations.md → Deleting one document | Ч | tools-and-api.md → Knowledge → delete | П |

Итог: ДО — 5 отсутствовали, 40 частично, 0 полностью.
ПОСЛЕ — 45 полностью, 0 частично, 0 отсутствует.

Отдельно: `/mcp` (POST/GET) не входит в OpenAPI и не является Pi JS
инструментом; он описан в `docs/mcp-chatgpt.md`, но не в `capabilities.py`
(см. P1 в разделе 7).

## 4. Найденные пробелы и добавленные фрагменты

### Часть 1 (подтверждённые факты)

1. `docs/handbook/task-lifecycle.md` → Status values:
   - добавлена строка `archived` (кто ставит: `update_task` /
     `PATCH /tasks/{task_id}`; терминальный, сохраняется, считается
     `GET /tasks/counts`, human number не меняется, воркер не клеймит,
     т.к. claim только по `in_progress`; не путать с архивным профилем
     агента);
   - добавлено явное пояснение, что `status` — free-form (`TaskPatch.status:
     str | None`, `TaskRepository.update` пишет `str(status)`), а таблица
     перечисляет конвенциональные значения, а не единственно допустимые;
   - указаны источники: `repository.py` (`update`, `claim_in_progress`,
     `get_status_counts`) и `app.py` (`PATCH /tasks/{task_id}`,
     `GET /tasks/counts`).
2. `task-lifecycle.md` → Resume a session:
   - resume разрешён для терминальных статусов; инструмент отказывает
     только `failed`;
   - `PATCH` в `in_progress` — это авторизация исполнения, а не просто
     дописывание инструкций;
   - следующий claim запускает новый цикл; при неизменных executor/workspace
     продолжается прежняя native-сессия, поэтому счётчик попыток растёт
     (наблюдался `attempt 7` после `done`).
3. `docs/handbook/tools-and-api.md` → Deferred operations and Git outcomes:
   - `git.commit_push` при чистом дереве: `succeeded`,
     `action=commit_local`, `message="Нет новых изменений для фиксации
     (рабочая директория чиста)."`, `commit_hash=null`, `pushed=false`,
     `task_status=done`;
   - это не ошибка и не повод повторять операцию; проверять `git log` /
     `git show HEAD`;
   - пустой коммит не создаётся; при `push=false` push не выполняется;
   - отдельно отмечено поведение `push=true` без нового коммита
     (`commit_and_push` всё равно делает `git push`, `action=commit_push`).
4. `docs/handbook/verification.md` → Git state inspection (read-only):
   - добавлены команды `git status --porcelain=v1 -uall`,
     `git diff --cached --name-status`, `git diff --name-status`,
     `git show --stat --name-status HEAD`, `git log --oneline -5`,
     `git ls-tree -r --name-only HEAD | grep <expected-path>`,
     `git cat-file -e HEAD:<path>`, `git ls-files -- <path>`,
     `git stash list`;
   - отмечено, что `.git` в песочнице обычно read-only: чтение работает,
     `git add`/`index.lock` даёт `Read-only file system`, это норма.
   - `docs/handbook/objects.md` → Task: явно разведены
     `metadata.changed_files` (согласованный список путей задачи) и
     `metadata.provenance.changed_files` (последняя сессия; наблюдался
     `[".coverage"]` при 10 корректных путях задачи); указано, что
     `GET /tasks/{id}/session` предпочитает task-level и откатывается к
     provenance только при отсутствии первого.

### Часть 2 (аудит API → handbook)

Не было в таблице REST endpoints (8):
`POST /ask`, `POST /conversations/ask`,
`GET /tasks/{task_id}/artifacts`,
`GET /tasks/{task_id}/artifacts/{artifact_name}`,
`GET /tasks/{task_id}/session`,
`POST /tasks/{task_id}/subscriptions`,
`DELETE /tasks/{task_id}/subscriptions`,
`GET /telegram/links/{chat_id}/{telegram_message_id}`.

Из них нигде в хендбуке не были описаны 5 (`/ask`, `/conversations/ask`,
три artifact/session-маршрута). Подписки были фрагментом
`task-lifecycle.md`, а Telegram-link — фрагментом
`knowledge-and-integrations.md`, но без параметров, тела, кодов и полного
набора полей.

Системные пробелы у 40 «частичных» маршрутов:
- нигде не было тел запросов с типами/обязательностью/`extra=forbid`;
- нигде не было полных списков полей ответа (были только имена
  `response_model` в `capabilities.py`);
- коды `401/403/404/409/422/503` не были систематизированы;
- не были перечислены path/query-параметры (кроме пары мест);
- не были связаны состояния: task, turn, operation, subscription, review
  verdict, recommendation;
- не было матрицы «маршрут → раздел хендбука».

Добавлено в `docs/handbook/tools-and-api.md`:
- полная таблица 45 маршрутов с колонкой required action;
- `### Common conventions`: auth, collection-resource, limit/offset/ordering,
  422/500, free-form task status;
- `### Response representations`: все 20+ схем ответов и их поля/статусы;
- по подразделу на каждый маршрут: path/query, тело, ответ, ошибки,
  идемпотентность, связанные состояния;
- `## Deferred operations and Git outcomes` (часть 1, п. 3);
- `call_api` в списке инструментов; счётчик 37 → 38;
- static fallback для discovery при отсутствии `.venv`.

Также:
- `capability-inventory.md`: заголовок 38 tools, добавлена строка
  `call_api` (Behavior/Backend/Result/Permission), сверены все 38 имён;
- `README.md`: строка индекса `tools-and-api.md` теперь отражает полный
  endpoint reference;
- `docs/api.md`: убран несуществующий
  `GET /conversations/{id}/turns/active`, добавлен реальный
  `GET /turns/{turn_id}`; `idempotency_key` заменён на `external_id` для
  сообщений; добавлены коды 201/200/409 и ссылка на полный справочник.

Дополнительно найдено и исправлено при аудите состояний (не было в задании,
но прямо влияет на «все состояния»):
- фактический статус задачи — `waiting_for_input`, а не `waiting_user_input`
  (`TaskExecutionWorker`, `TaskRepository`, тесты). Исправлено в
  `task-lifecycle.md`, `knowledge-and-integrations.md`, `review.md`,
  `docs/api.md`.
- во внутреннем review-цикле распознаётся JSON-вердикт `approved` /
  `changes_requested` (`src/kuzya/review_executor.py`), а не текстовые
  маркеры `ВЕРДИКТ: ОДОБРЕНО` / `ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ`.
  `task-lifecycle.md` исправлен и ссылается на JSON-контракт `review.md`.

## 5. Изменённые файлы (только документация)

- `docs/handbook/task-lifecycle.md`
- `docs/handbook/tools-and-api.md`
- `docs/handbook/verification.md`
- `docs/handbook/objects.md`
- `docs/handbook/capability-inventory.md`
- `docs/handbook/knowledge-and-integrations.md`
- `docs/handbook/review.md`
- `docs/handbook/README.md`
- `docs/api.md`

Diff: `/workspace/outbox/changes.diff` (и копия в `/outbox/changes.diff`).
Новых файлов в `docs/handbook/` не создавалось: реестр `HANDBOOK` в
`src/kuzya/capabilities.py` — код, менять его в этой задаче нельзя, а файл
вне реестра не попал бы в discovery-индекс. Поэтому всё добавлено в
существующие документы.

## 6. Выполненные проверки

| Проверка | Команда | Результат |
|---|---|---|
| Полнота таблицы маршрутов | stdlib-скрипт: regex `@app.<method>("<path>")` из `app.py` vs строки таблицы | app: 45 unique, doc: 45 unique, `missing in doc: []`, `extra in doc: []` — exit 0 |
| Инвентарь маршрутов | stdlib-скрипт: `_endpoint(...)` + генератор subscriptions из `capabilities.py` vs `app.py` | 43 явных + 2 subscriptions = 45; `missing from capabilities: []`, `extra: []` — exit 0 |
| Инвентарь инструментов | `diff` отсортированных имён из таблицы `capability-inventory.md` и списка из `policy.mjs` | `SAME`, по 38 строк — exit 0 |
| Ссылки/анкоры handbook | stdlib-скрипт: все `[...](*.md#anchor)` | единственное «совпадение» — `../mcp-chatgpt.md`, файл существует (`docs/mcp-chatgpt.md`) |
| Пробелы/whitespace | `git diff --check` | exit 0, вывода нет |
| Изменённые пути | `git status --porcelain=v1 -uall` | только 9 docs-файлов (`M`), код/тесты не тронуты |
| Pytest parity | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_capabilities.py -q` | **не выполнен**: `.venv/bin/python: No such file or directory` |
| OpenAPI | команда из `tools-and-api.md` | **не выполнен**: нет `.venv`; системный `python3` без `fastapi` (`ModuleNotFoundError`) |
| Node tool enumeration | `node tests/policy_tool_names.mjs src/pi_runner/policy.mjs` | **не выполнен**: `ERR_MODULE_NOT_FOUND: Cannot find package 'typebox'` |
| markdownlint | — | инструмент не установлен; `npx`-установка не выполнялась |

## 7. Остаточные риски, P1-находки и что требует решения человека

### P1 — инвентарное расхождение (код не менялся)

`/mcp` (`POST /mcp`, `GET /mcp`) смонтирован в приложении
(`src/kuzya/api/mcp/endpoint.py`, вызов `attach_mcp_endpoint(app)` в конце
`create_app`), но:
- отсутствует в `src/kuzya/capabilities.py`;
- не виден `audit_capabilities`, потому что не попадает в OpenAPI `paths`;
- в хендбуке его не было (описан только в `docs/mcp-chatgpt.md`);
- execution-токены песочниц к нему не допускаются (нет правила в
  `resolve_requirement`), поэтому это интеграционная, а не агентская
  поверхность.

Это не исправлялось (запрет на код). Нужно решение человека: объявлять ли
`/mcp` в инвентаре отдельным типом capability или явно зафиксировать его
как «вне инвентаря» в политике аудита. Пока в `tools-and-api.md` добавлено
явное примечание о его существовании и области действия.

### P2 — расхождения, не влияющие на поведение

- Комментарий в `src/kuzya/capabilities.py` («37 tools plus every REST
  endpoint») устарел: фактически 38 инструментов. Документация исправлена,
  код-комментарий — нет (код не трогали).
- `docs/mcp-chatgpt.md` ссылается на `src/agent_manager/api/mcp/` и
  переменные `AGENT_MANAGER_*`; фактический модуль — `src/kuzya/api/mcp/`.
  Не проверял и не правил: вне области задачи.
- `docs/api.md` остаётся кратким русскоязычным обзором, а не полным
  справочником; теперь он явно ссылается на
  `docs/handbook/tools-and-api.md` как на полный источник. Исправлены
  только обнаруженные ложные утверждения (turns/active, idempotency_key).

### Остаточные риски аудита

- Матрица построена статически. Runtime-сверка OpenAPI не выполнялась из-за
  отсутствия `.venv` и `typebox`; коды ответов взяты из явных
  `HTTPException` + стандартного FastAPI `422`, а не из живых запросов.
- Факт `attempt 7` и содержимое `provenance.changed_files=[".coverage"]`
  взяты из наблюдавшейся сессии в задании и не воспроизводились в этом
  окружении; код-путь, объясняющий оба факта, проверен.
- `GET /tasks/{id}/artifacts/{name}` всегда отдаёт `text/plain;
  charset=utf-8`, даже для бинарных артефактов. Это задокументировано как
  факт, но может быть неожиданностью для интеграторов; вопрос к владельцу
  API, не к документации.
- Проверка markdown-стиля не автоматизирована (нет markdownlint).
- `git commit`/`push` не выполнялись; diff лежит в outbox.

## 8. Артефакты

- `/workspace/outbox/report.md` — этот отчёт.
- `/workspace/outbox/changes.diff` — полный diff только по документации.
- Копии: `/outbox/report.md`, `/outbox/changes.diff`.

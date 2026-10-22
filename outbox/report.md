# Отчёт: исправление замечаний ревью №75 (P1 + 4 P2)

Дата: 2026-10-22. Задача: `79fed199a9044b57841c6bc8edacc1f5`.
Область: только handbook и инвентарь возможностей. Код auth/middleware,
схемы, роуты, миграции и поведение сервиса не менялись. Commit, push,
deploy и запуск постоянных сервисов не выполнялись.

## 0. Итог

- P1: `/mcp` и `/mcp/` добавлены в `src/kuzya/capabilities.py` как четыре
  явные записи-адаптера (`GET`/`POST`), без дублирования 43 MCP-инструментов.
  Аудит больше не считает их ни `missing`, ни `extra`: REST-записи сверяются
  с OpenAPI, адаптеры — с фактически смонтированными путями приложения, а
  `resolve_requirement` для адаптера обязан оставаться `None`.
- P2-1: resume из `failed` описан корректно: отказ инструмента связан только
  с ошибкой чтения API, а не со статусом задачи.
- P2-2: раздел про no-op commit переписан: чистое дерево само по себе даёт
  `success=False` до `git commit`, а успешный `succeeded` с
  `commit_hash=null` возможен только в узком случае объявленных пригодных
  путей и ответа git "nothing to commit".
- P2-3: зафиксировано исключение `GET /tasks/{task_id}/analysis` — только
  внутренний UUID, human number не резолвится.
- P2-4: добавлены типы, обязательность и поведение `null` для `TaskPatch`,
  `AgentPatch`, query `limit`/`offset` и path `revision`; завышенное
  утверждение матрицы покрытия «45 полностью» снято.
- Проверки: `31 passed, 1 skipped`, exit `0`; `git diff --check` — exit `0`.

## 1. Окружение и обход битого `.venv`

Штатная команда из задания не запускается: `.venv/bin/python` — симлинк на
несуществующий интерпретатор
`/home/itolstov/.local/share/uv/python/cpython-3.14-linux-x86_64-gnu/bin/python3.14`.
Фактический вывод:

```text
/bin/bash: line 1: .venv/bin/python: No such file or directory
```

Код выхода: `127`. Поэтому использован временный venv в `/tmp`, как
разрешено в задании:

```bash
UV_CACHE_DIR=/tmp/kuzya-uv-cache \
UV_PROJECT_ENVIRONMENT=/tmp/kuzya-venv \
/home/itolstov/.local/bin/uv sync --no-install-project
```

Результат: временный venv создан, exit `0`. Ссылки на `/tmp/kuzya-venv`
ниже означают именно этот интерпретатор.

## 2. Правки по пунктам

### P2-1. Resume из `failed` — `docs/handbook/task-lifecycle.md:151-164`

Было ошибочно указано, что `resume_task_session` отказывает задаче со
статусом `failed`. Исправлено:

- отказ описан только как ошибка чтения задачи через API: `backendRequest`
  возвращает конверт `{status: "ok"|"failed", data}`, и проверяется
  `current.status` (`src/pi_runner/policy.mjs:66-85`);
- явно указано, что статус задачи находится в `current.data.status` и не
  проверяется, а `PATCH` выполняется без проверки прежнего статуса
  (`policy.mjs:564-573`);
- resume из `failed` разрешён и перезапускает исполнение наравне с
  `done`/`archived`; `PATCH` ставит `status="in_progress"` и добавляет
  инструкции;
- предупреждение о подтверждении намерения расширено на `failed`.

### P2-2. No-op commit — `docs/handbook/tools-and-api.md:653-701`

Раздел переименован в «Clean tree and no-op commit outcomes» и уточнён по
коду:

- при пустом/отсутствующем `metadata.changed_files` используется fallback
  `git status --porcelain` с исключением `localdoc/` и `.gemini/`
  (`src/kuzya/git_engine.py:96-113`);
- если кандидатов нет (обычное чистое дерево), возвращается
  `success=False` до `git commit`; обработчик превращает это в
  `OperationExecutionError`, и операция завершается `failed`
  (`src/kuzya/operation_handlers.py:106-109`);
- список объявленных путей, где все пути непригодны, также даёт
  `success=False`;
- успешный `succeeded` с `commit_hash=null` возможен только при хотя бы
  одном пригодном объявленном пути и ответе `git commit` "nothing to commit"
  (`git_engine.py:163-170`);
- отдельно разведено поведение `push=true`: при провале clean-tree/no-
  stageable `commit_and_push` возвращается до `git push` и пуш не выполняет;
  в узком успешном no-op варианте с объявленными путями код может дойти до
  `git push` текущей ветки, но нового коммита там нет. Это ограничивает
  прежнее безусловное утверждение и не выдумывает несуществующее поведение.

### P2-3. Human number не везде — `docs/handbook/tools-and-api.md:20-29, 60, 451-456`

- Общее правило `{task_id}` ограничено маршрутами, которые резолвят задачу
  через `TaskRepository.get`.
- Явно описано исключение `GET /tasks/{task_id}/analysis`: handler не
  вызывает `TaskRepository.get`, а передаёт строку прямо в
  `AnalysisRepository.list_reports` (`src/kuzya/api/app.py:1716-1724`).
  Для задачи с UUID `A` и `number=41` запрос `/tasks/41/analysis` вернёт
  пустой список.
- Числовые фильтры других сущностей не объявлены поддерживаемыми: отмечено,
  что `task_id` в `/recommendations` и `target_id` операций — opaque strings.
- Строка таблицы для `/tasks/{task_id}/analysis` дополнена пометкой
  «internal UUID only, no human-number lookup».

### P2-4. Типы, обязательность и nullable — `docs/handbook/tools-and-api.md`

- `limit`/`offset` (`tools-and-api.md:89-94`): `limit` — `int | null`,
  необязательный, фактический верхний bound — он сам; `offset` — `int`,
  default `0`. В сигнатурах нет `ge`/`le`, поэтому не заявлены
  несуществующие ограничения; отмечено применение `.limit()`/`.offset()` в
  repository.
- `TaskPatch` (`tools-and-api.md:242-256`): перечислены все типы
  (`str | null`, `object | null`, `array<TaskAttachment> | null`) и
  поведение null:
  - `description`/`workspace`/`agent_id` сохраняют null;
  - `attachments: null` удаляет `metadata.attachments`;
  - `metadata: null` — merge с `{}`, то есть no-op, не очищает ключи;
  - `title: null` и `status: null` не отклоняются, а `TaskRepository.update`
    пишет `str(value)`, поэтому сохраняется строка `"None"`.
- `AgentPatch` (`tools-and-api.md:534-542`): `expected_revision` обязательный
  `int`; опциональные `name`/`role`/`system_prompt` — `str | null`,
  `runner_config`/`knowledge` — `object | null`, `skills` —
  `array<object> | null`; явный null отклоняется `422`.
- `revision` в path (`tools-and-api.md:527-529`): `int` без объявленных
  числовых границ; не-целое даёт `422`.

### P1. `/mcp` в инвентаре — `src/kuzya/capabilities.py`

- `Capability` получил поле `resource`; добавлен `kind="adapter"` и helper
  `_adapter` (`capabilities.py:157-183, 241-258`).
- Добавлены четыре записи `GET`/`POST` × `/mcp`/`/mcp/` с `action="*"`,
  `resource="*"`, `doc="mcp.md"` (`capabilities.py:998-1030`). 43
  генерируемых MCP-инструмента не дублируются.
- `ENDPOINT_CAPABILITIES` включает и REST-endpoint, и adapter-записи;
  `ADAPTER_CAPABILITIES` выделяет последние (`capabilities.py:1031-1042`).
- `audit_capabilities` (`capabilities.py:1140-1255`):
  - сравнивает с OpenAPI только записи `kind="endpoint"` — адаптер не даёт
    ложный `unsupported-route`/`undocumented-route`;
  - при переданных `mounted_route_paths` требует наличия пути адаптера на
    роутере (`missing-adapter-route`);
  - не разрешает адаптеру пересекаться с OpenAPI
    (`adapter-openapi-overlap`);
  - требует `resolve_requirement(...) is None` для адаптера
    (`adapter-permission-drift`), то есть execution-токен по-прежнему
    получает `403`, а административный — те же права, что в REST.
- Auth/middleware и `_ROUTE_RULES` не менялись.
- Документация синхронизирована: `docs/handbook/mcp.md:28-35`,
  `docs/handbook/capability-inventory.md:105-114`,
  `docs/handbook/tools-and-api.md:10-18, 780-810`,
  `docs/handbook/verification.md:149`.
- Добавлен регрессионный тест `tests/test_capabilities.py:118-155`: проверяет
  состав адаптера, отсутствие `/mcp` в OpenAPI, `resolve_requirement is None`
  и то, что аудит замечает исчезнувший mounted route.

## 3. Корректировка матрицы покрытия

Матрица находилась не в handbook, а в предыдущем артефакте
`outbox/report-task75.md`. Её итог «ПОСЛЕ — 45 полностью» был завышен.
В этом отчёте он снят: полное покрытие не переподтверждается для всех 45
маршрутов без отдельной сверки.

| Маршрут / группа | Прежняя отметка | Скорректированная отметка |
|---|---|---|
| `GET /tasks/{task_id}/analysis` | П | П: явно описано, что нужен внутренний UUID и human number не резолвится |
| `PATCH /tasks/{task_id}` | П | П по типам и null-поведению запроса |
| `PATCH /agents/{agent_id}` | П | П по типам, обязательности `expected_revision` и запрету null |
| `GET /agents/{agent_id}/revisions/{revision}` | П | П по типу path `revision` |
| list-маршруты с `limit`/`offset` | П | П только по типам/дефолтам query; без выдуманных границ |
| остальные маршруты | П | не пересертифицированы в этой задаче; прежняя всеобщая отметка не поддерживается |

## 4. Изменённые файлы

Handbook и инвентарь:

- `docs/handbook/task-lifecycle.md`
- `docs/handbook/tools-and-api.md`
- `docs/handbook/mcp.md`
- `docs/handbook/capability-inventory.md`
- `docs/handbook/verification.md`
- `src/kuzya/capabilities.py`

Тест (для P1, не production-код):

- `tests/test_capabilities.py`

Артефакты этой задачи: `outbox/report.md`, `outbox/changes.diff`.
`outbox/` в коммит не включался; коммит не создавался.

## 5. Проверки

| Команда | Результат | Exit |
|---|---|---|
| `PYTHONPATH=src .venv/bin/python -m pytest tests/test_capabilities.py tests/test_mcp.py -q` | не запускалось: `.venv/bin/python: No such file or directory` | `127` |
| `UV_CACHE_DIR=/tmp/kuzya-uv-cache UV_PROJECT_ENVIRONMENT=/tmp/kuzya-venv /home/itolstov/.local/bin/uv sync --no-install-project` | временный venv создан | `0` |
| `PYTHONPATH=src /tmp/kuzya-venv/bin/python -m pytest tests/test_capabilities.py tests/test_mcp.py -q -o addopts=` | `31 passed, 1 skipped in 5.27s` | `0` |
| `PYTHONPATH=src /tmp/kuzya-venv/bin/python -m pytest tests/test_capabilities.py tests/test_mcp.py -q` | точки `...........s....................`, без падений | `0` |
| сверка OpenAPI ↔ инвентарь ↔ handbook (скрипт ниже) | 45 REST ↔ 45 endpoint-kind ↔ 45 строк таблицы; missing/extra пусты; 4 adapter-записи, оба пути смонтированы; `/mcp` отсутствует в OpenAPI | `0` |
| `git diff --check` | пустой вывод | `0` |

Сверочный скрипт (фактический вывод):

```text
OpenAPI REST operations: 45
endpoint-kind capabilities: 45
handbook REST table rows: 45
missing from inventory: []
extra in inventory: []
missing from handbook: []
extra in handbook: []
adapter records: [('GET', '/mcp'), ('GET', '/mcp/'), ('POST', '/mcp'), ('POST', '/mcp/')]
adapter paths mounted: ['/mcp', '/mcp/']
/mcp in OpenAPI: False
```

Один skip — существующий guard `test_deferred_policy_tools_return_structured_pending`
(Node + Pi TypeBox runtime). Это не pass, но и не регрессия этой задачи;
остальные 31 тест прошли.

## 6. Остаточные риски

- Записи адаптера имеют `action="*"`/`resource="*"` как описание
  наследуемой authority, а не новое право. Инвариант держится на отсутствии
  `/mcp` в `_ROUTE_RULES`; аудит теперь падает при `resolve_requirement`,
  отличном от `None`, и при исчезновении mounted path. Внутренний REST-вызов
  по-прежнему повторно проверяет конкретный action/resource.
- Проверка mounted route выполняется по путям: Starlette `Route` для `/mcp`
  не задаёт `methods` и принимает методы транспорта; инвентарь явно
  фиксирует протокольные `GET`/`POST`.
- В узком успешном no-op случае (`push=true`, объявленные пригодные пути,
  git "nothing to commit") код может выполнить `git push` существующей
  ветки без нового коммита. Документация это явно ограничивает; чистое
  дерево без пригодных путей до push не доходит.
- Матрица покрытия пересертифицирована только по пунктам ревью; полное
  покрытие остальных маршрутов в этой задаче заново не доказывалось.
- Штатный `.venv` сломан (симлинк на отсутствующий Python 3.14); проверки
  выполнены во временном venv, как разрешено заданием.

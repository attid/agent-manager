# Agent Manager REST API Specification

REST API сервиса Agent Manager предоставляет полный интерфейс для управления задачами, диалогами, сообщениями, очередью выполнения и профилями агентов.

- **Сетевой адрес по умолчанию**: `http://127.0.0.1:18767`
- **Протокол**: HTTP/1.1 JSON (REST)
- **OpenAPI Schema**: `http://127.0.0.1:18767/openapi.json`
- **Интерактивная документация (Swagger UI)**: `http://127.0.0.1:18767/docs`

---

## 1. Общие принципы и заголовки

1. **Изоляция сети**: API слушает исключительно loopback-интерфейс (`127.0.0.1`).
2. **Correlation ID**: Любой запрос может передавать заголовок `X-Request-ID`. Если он не передан, сервер генерирует UUID v4 и возвращает его в ответе:
   ```http
   X-Request-ID: c56a4180-65aa-42ec-a945-5fd21dec0538
   ```
3. **Хранилище**: SQLite в режиме Write-Ahead Logging (WAL) с сериализованной записью и параллельным чтением. Все схемы версионируются через Alembic.
4. **Формат ошибок**:
   ```json
   {
     "detail": "Сообщение об ошибке"
   }
   ```

---

## 2. Жизненный цикл задач (Task Lifecycle)

Статусы задач:
- `incoming` — новая задача, ожидает анализа или назначения.
- `in_progress` — задача взята в работу `TaskExecutionWorker`.
- `waiting_user_input` — разработчик запросил уточнение (через `ask_clarification`), требуется ответ человека.
- `ready_to_commit` — код написан, тесты прошли, Code Review пройден успешно. Задача ожидает явного подтверждения человека (Commit Local / Commit & Push / Rollback).
- `done` — задача успешно закоммичена, задеплоена или подтверждена.
- `failed` — ошибка рантайма, упавшие тесты или необработанное исключение.
- `cancelled` — задача отменена или изменения откачены (`rollback`).

### Блокировки и изоляция воркспейсов:
Если в каком-либо воркспейсе есть задача в статусе `in_progress` или `ready_to_commit`, новые задачи для этого воркспейса не берутся воркером в работу до разрешения блокировки. Задачи для других воркспейсов выполняются параллельно без задержек.

---

## 3. Эндпоинты Задач (Tasks)

### 3.1. Создание задачи: `POST /tasks`
Создает новую задачу.

**Запрос:**
```bash
curl -X POST http://127.0.0.1:18767/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Реализовать проверку статуса CI",
    "description": "Интегрировать вызов gh run watch в TaskWorker",
    "workspace": "agent-manager",
    "status": "incoming",
    "metadata": {
      "task_type": "coding",
      "requires_review": true
    }
  }'
```

**Ответ (`201 Created`):**
```json
{
  "id": "e6a2bc41-76a0-4382-b7b5-2fa09990e1cb",
  "title": "Реализовать проверку статуса CI",
  "description": "Интегрировать вызов gh run watch в TaskWorker",
  "status": "incoming",
  "workspace": "agent-manager",
  "metadata": {
    "task_type": "coding",
    "requires_review": true
  },
  "created_at": "2026-10-18T02:00:00Z",
  "updated_at": "2026-10-18T02:00:00Z"
}
```

### 3.2. Список задач: `GET /tasks`
Возвращает список задач с фильтрацией и пагинацией.

**Параметры query:**
- `status` (string, опционально): фильтр по статусу (`incoming`, `in_progress`, `ready_to_commit`, `done`, `failed`).
- `workspace` (string, опционально): фильтр по воркспейсу (например `agent-manager`).
- `limit` (int, опционально): ограничение количества записей.
- `offset` (int, по умолчанию 0): смещение выборки.

**Пример:**
```bash
curl "http://127.0.0.1:18767/tasks?status=ready_to_commit&workspace=agent-manager"
```

### 3.3. Получение задачи по ID: `GET /tasks/{task_id}`
**Пример:**
```bash
curl http://127.0.0.1:18767/tasks/e6a2bc41-76a0-4382-b7b5-2fa09990e1cb
```

### 3.4. Обновление задачи: `PATCH /tasks/{task_id}`
Частичное обновление полей задачи (`title`, `description`, `status`, `workspace`, `metadata`).

**Пример:**
```bash
curl -X PATCH http://127.0.0.1:18767/tasks/e6a2bc41-76a0-4382-b7b5-2fa09990e1cb \
  -H "Content-Type: application/json" \
  -d '{
    "status": "in_progress"
  }'
```

### 3.5. Пакетный импорт задач: `POST /imports/tasks`
Идемпотентный импорт истории задач. Существующие задачи пропускаются без изменений.

---

## 4. Эндпоинты Диалогов и Сообщений (Conversations & Messages)

### 4.1. Создание диалога: `POST /conversations`
```bash
curl -X POST http://127.0.0.1:18767/conversations \
  -H "Content-Type: application/json" \
  -d '{"title": "Обсуждение архитектуры Sprint 3"}'
```

### 4.2. Список диалогов: `GET /conversations`
```bash
curl "http://127.0.0.1:18767/conversations?limit=20&offset=0"
```

### 4.3. Добавление сообщения в диалог: `POST /conversations/{id}/messages`
Идемпотентное добавление сообщения с опциональным `idempotency_key`.

```bash
curl -X POST http://127.0.0.1:18767/conversations/{id}/messages \
  -H "Content-Type: application/json" \
  -d '{
    "role": "user",
    "text": "Как запустить сервис в production?",
    "idempotency_key": "msg-unique-key-100",
    "metadata": {"media_type": "text"}
  }'
```

### 4.4. Получение сообщений диалога: `GET /conversations/{id}/messages`
```bash
curl "http://127.0.0.1:18767/conversations/{id}/messages?limit=50"
```

---

## 5. Эндпоинты Очереди Ходов Секретаря (Turns)

Используются Telegram-ботом и диспетчером для строгой пошаговой обработки (один активный ход на диалог).

- `POST /conversations/{id}/turns` — постановка сообщения в очередь ходов.
- `GET /conversations/{id}/turns/active` — получение текущего активного хода.

---

## 6. Эндпоинты Профилей Агентов (Agent Profiles)

Профили агентов версионируются иммутабельно (append-only) с защитой от параллельных правок (`expected_revision`).

### 6.1. Список текущих агентов: `GET /agents`
```bash
curl http://127.0.0.1:18767/agents
```

### 6.2. Создание агента: `POST /agents`
Создает первую ревизию (revision 1) агента.

```bash
curl -X POST http://127.0.0.1:18767/agents \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "crypto-bot-developer",
    "name": "Разработчик Crypto Bot",
    "role": "developer",
    "system_prompt": "Ты разрабатываешь Telegram-бота для криптоплатежей на Python.",
    "runner_config": {
      "tools": ["read", "write", "edit", "bash"],
      "network": true
    },
    "skills": [{"skill": "codespaces"}]
  }'
```

### 6.3. Получение текущего профиля: `GET /agents/{agent_id}`
```bash
curl http://127.0.0.1:18767/agents/system-secretary
```

### 6.4. Обновление агента: `PATCH /agents/{agent_id}`
Создает следующую ревизию. Требует передачи `expected_revision`. При несовпадении возвращает `409 Conflict`.

```bash
curl -X PATCH http://127.0.0.1:18767/agents/crypto-bot-developer \
  -H "Content-Type: application/json" \
  -d '{
    "expected_revision": 1,
    "system_prompt": "Обновленный системный промпт...",
    "runner_config": {
      "tools": ["read", "write", "edit", "bash"]
    }
  }'
```

### 6.5. История ревизий: `GET /agents/{agent_id}/revisions`
Возвращает полный лог всех ревизий агента с временными метками.

---

## 7. Интеграция с OpenViking

В рантайме агентам доступен сервис OpenViking:
- **Базовый URL**: `http://127.0.0.1:1933`
- **Авторизация**: `Authorization: Bearer <token>` (автоматически пробрасывается в `backend_api.json`)
- **Поиск**:
  ```bash
  curl -X POST http://127.0.0.1:1933/api/v1/search/search \
    -H "Authorization: Bearer $OPENVIKING_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"query": "aiogram middleware", "limit": 5}'
  ```
- **Чтение содержимого**:
  ```bash
  curl "http://127.0.0.1:1933/api/v1/content/read?uri=viking://resources/doc_id" \
    -H "Authorization: Bearer $OPENVIKING_TOKEN"
  ```

## Выбор исполнителя задачи и проверка workspace

`POST /tasks` и `PATCH /tasks/{id}` принимают nullable `agent_id`; GET возвращает
его отдельным полем. NULL сохраняет совместимость со старым default executor.
`workspace` и `agent_id` независимы. Хранилище не ограничивает свободные статусы
и исторические записи; фактическое исполнение проверяет профиль и каталог.

`GET /workspaces/resolve?workspace=/absolute/path&agent_id=system-scout` не меняет
данные. Ответ: `host_path`, `sandbox_path=/workspace`, `agent_id`,
`profile_revision`, `project_read_only`, `network`. Ошибки: 404 для отсутствующего
агента, 409 для архивного, 422 для отсутствующего или недопустимого каталога.
Если workspace отсутствует, берётся project профиля, затем default workspace.
Инструменты секретаря используют этот endpoint до создания/запуска задачи.

Для новой версии требуется миграция `0006_task_agent`. Она добавляет только
nullable колонку, не переписывает старые задачи. Startup API откажется работать
с БД без `tasks.agent_id` и предложит применить миграции. Проверки миграции
выполняются на временных БД; обновление production выполняется отдельно.

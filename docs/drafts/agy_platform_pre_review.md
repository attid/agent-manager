# Отчет независимого предварительного ревью реализации Agent Manager Platform Completion

- **Объект ревью:** Коммиты `fd62693..3af8653` (HEAD: `3af86532748c2ca94bd1f64aa985cdc2f65d9559`).
- **Сфера проверки:** Контракты T01–T07, `docs/plans/platform-completion/README.md`, `docs/e2e-pirozhki-test-scenario.md`, исходный код `src/agent_manager`, `src/pi_runner`, миграции Alembic и тесты.
- **Статус / Вердикт:** **ОТКЛОНЕНО (UNAPPROVED / BLOCKED)**.
  Реализация содержит критические блокирующие дефекты уровня **P1**, делающие невозможным завершение цикла доработки кода (code review fix loop) и нарушающие изоляцию прав исполнения (execution privilege escalation), а также дефекты уровня **P2** в логике Telegram-интерфейса и безопасности путей базы знаний. Сдача этапа T07 и запуск E2E невозможны до устранения выявленных дефектов.

---

## 1. Действенные замечания (Actionable Findings)

### P1 Findings (Критические блокирующие дефекты и проблемы безопасности)

#### Дефект 1: Фатальный сбой цикла исправления замечаний ревьюера (Developer Resume Loop Broken)
- **Файл и строки:** [`src/agent_manager/task_worker.py:664-674`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/task_worker.py#L664-L674)
- **Следствие:**
  Когда ревьюер возвращает вердикт `CHANGES_REQUESTED`, метод `_run_review_cycle` пытается перезапустить разработчика для устранения замечаний (раунды 2 и 3). Формируется команда:
  ```python
  dev_cmd = [
      sys.executable, "-m", "pi_runner", "resume",
      "--state-dir", str(state_dir),
      "--agent", str(dev_role_file),
      "--task", str(review_task_file),
  ]
  ```
  Однако в CLI парсере `pi_runner` аргумент `--session` объявлен как строго обязательный ([`src/pi_runner/cli.py:332`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/pi_runner/cli.py#L332): `parser.add_argument("--session", required=True)`).
  При вызове команды подпроцесс немедленно завершается с кодом ошибки 2:
  `pi_runner: error: the following arguments are required: --session`
  На строке [`task_worker.py:683`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/task_worker.py#L683) выбрасывается `RuntimeError`, и задача завершается аварийно.
  **Разработчик в принципе не может возобновить работу и исправить замечания ревьюера.**
- **Воспроизведение и доказательства:**
  Прямой запуск CLI runner resume без `--session`:
  ```bash
  PYTHONPATH=src .venv/bin/python -m pi_runner resume --state-dir /tmp --agent /tmp/role.json --task /tmp/task.md
  ```
  Вывод:
  ```
  usage: pi_runner resume [-h] --session SESSION ...
  pi_runner: error: the following arguments are required: --session
  ```
  Код возврата: `2`.
  В существующих тестах дефект оставался скрытым из-за полного мокирования `_run_review_cycle` (например, `tests/test_task_worker.py:270` `mocker.patch.object(worker, "_run_review_cycle", return_value={"passed": True})`).
- **Исправление:**
  Сохранять идентификатор сессии `session_id = runner_result.get("session_id")` после первичного выполнения задачи и передавать его в `_run_review_cycle`:
  ```python
  dev_cmd.extend(["--session", str(session_id)])
  ```

---

#### Дефект 2: Несанкционированная эскалация привилегий токенов исполнения (Privilege Escalation via MANAGEMENT_ACTIONS)
- **Файл и строки:** [`src/agent_manager/permissions.py:184-186`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/permissions.py#L184-L186)
- **Следствие:**
  В логике вычисления ресурсов для токенов исполнения:
  ```python
  if any(action in MANAGEMENT_ACTIONS for action in actions):
      resources = frozenset({ADMIN_RESOURCE})
  ```
  Константа `MANAGEMENT_ACTIONS` включает в себя `REVIEW_TRIGGER`, `ANALYSIS_TRIGGER`, `GIT_ACTIONS`, `TASKS_WRITE`.
  При старте воркера задачи дочернему агенту выдается токен с разрешенными действиями, включающими `trigger_review`, `trigger_commit_push`, `update_task` ([`task_worker.py:297-307`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/task_worker.py#L297-L307)). Из-за приведенной выше проверки агент задачи `t1` получает ресурс `ADMIN_RESOURCE` (`"*"`).
  Это дает дочернему агенту задачи неограниченный доступ: он может запускать операции (ревью, анализ, git push) для **любых чужих задач** (`task:t2`), модифицировать состояние чужих задач и обходить границы инстансов и задач.
- **Воспроизведение и доказательства:**
  С помощью FastAPI `TestClient`: выпускается токен исполнения для `task_id="t1"` с правами `{"trigger_review"}`.
  Отправляется запрос на создание операции для чужой задачи:
  `POST /operations` с телом `{"kind": "review", "task_id": "t2"}` и данным токеном.
  Запрос возвращает статус `202 Accepted` вместо ожидаемого `403 Forbidden`.
  Существующий тест `test_scoped_grant_enforces_target_resource` (`tests/test_permissions.py:90`) проходил только потому, что передавал искусственный `resources=frozenset({"task:t1"})` напрямую в `issue_token`, не используя реальную функцию `resources_for_execution`.
- **Исправление:**
  В функции `resources_for_execution(task_id, actions)` не устанавливать `ADMIN_RESOURCE`, если задан `task_id`. Для действий, привязанных к задаче (`REVIEW_TRIGGER`, `ANALYSIS_TRIGGER`, `GIT_ACTIONS`, `TASKS_WRITE`), разрешенным ресурсом должен быть строго `frozenset({f"task:{task_id}"})`.

---

### P2 Findings (Функциональные дефекты и несоответствия спецификации)

#### Дефект 3: Бесконечный цикл pending и паразитная генерация запусков модели в Telegram `/analyze`
- **Файл и строки:** [`src/agent_manager/telegram/handlers.py:186-214`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/telegram/handlers.py#L186-L214)
- **Следствие:**
  1. Хэндлер `cmd_analyze` при каждом вызове безусловно выполняет `client.trigger_analysis(task_id)` без передачи `idempotency_key`. Так как в реальной среде операции выполняются асинхронно через фоновую очередь `operation_worker`, проверка статуса на строке 190 сразу возвращает `status == "pending"`. Бот отправляет сообщение: `"⏳ Анализ запущен... повторный просмотр не запускает модель"`.
  2. Когда пользователь позже повторно вызывает `/analyze <task_id>`, чтобы прочитать готовый отчет, хэндлер **не проверяет** готовые отчеты в хранилище, а снова вызывает `client.trigger_analysis(task_id)` без ключа идемпотентности. Это создает **еще одну новую операцию** в статусе `pending`, и бот снова отвечает заглушкой ожидания.
  **Пользователь никогда не может увидеть готовый отчет через команду `/analyze`**, а каждый ввод команды порождает повторные неконтролируемые вызовы LLM.
- **Воспроизведение и доказательства:**
  Анализ кода `handlers.py:186`: `trigger_analysis` вызывается до запроса отчетов `list_analysis_reports`.
  Тест `tests/telegram/test_analysis.py:27-58` маскировал проблему, так как `StubClient.get_operation` искусственно возвращал `status="succeeded"` синхронно.
- **Исправление:**
  В начале `cmd_analyze` запросить список существующих отчетов через `client.list_analysis_reports(task_id)`. Если готовый отчет найден — сразу отобразить его. Если отчета нет — запускать операцию анализа с детерминированным ключом идемпотентности, например `idempotency_key=f"analyze:{task_id}"`.

---

#### Дефект 4: Отсутствие валидации Path Traversal в путях базы знаний
- **Файл и строки:** [`src/agent_manager/analysis.py:761-768`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/analysis.py#L761-L768), [`src/agent_manager/knowledge.py:855-874`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/knowledge.py#L855-L874)
- **Следствие:**
  В функции `_knowledge_target_path(knowledge_base, target_kind, target_id)` путь формируется прямой конкатенацией:
  `knowledge_base.projects_dir / target_id / "summary.md"` или `components_dir / target_id / "rules.md"`.
  В методах `create_or_update_project_summary` и `update_component_rules` отсутствует проверка на выход за пределы корневого каталога базы знаний (`path.resolve().is_relative_to(kb_root)`).
  При получении от модели рекомендации с `target_id = "../../agents"` или `target_id = "../../../tmp"` файл будет создан или перезаписан за пределами изолированной директории базы знаний.
- **Исправление:**
  Добавить строгую валидацию `target_id` (запрет символов `/`, `\`, `..`), а также проверку `dest.resolve().is_relative_to(knowledge_base.base_dir)` перед выполнением файловых операций.

---

### P3 Findings (Несоответствия интерфейсов и документации)

#### Дефект 5: Нарушение схемы вывода инструмента `get_task_logs` в `policy.mjs`
- **Файл и строки:** [`src/pi_runner/policy.mjs:421-458`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/pi_runner/policy.mjs#L421-L458)
- **Следствие:**
  Согласно [`src/agent_manager/capabilities.py:283`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/capabilities.py#L283) и документации `docs/handbook/tools-and-api.md:98`, инструменты платформы обязаны возвращать структурированный JSON с полями `{ status: "ok" | "failed", ... }`.
  Инструмент `get_task_logs` в `policy.mjs` возвращает сырой неструктурированный текст Markdown (`# Logs for task ...`). Потребители API инструментов, ожидающие стандартный структурированный JSON, завершаются с ошибкой разбора JSON.
- **Исправление:**
  Привести вывод инструмента `get_task_logs` к структурированному формату JSON (`{ status: "ok", task_id: ..., logs: [...] }`), аналогично `get_task_status` и `list_tasks`.

---

## 2. Анализ тестового покрытия и белые пятна (Test Gaps)

1. **Изоляция и запуск дочерних процессов `pi_runner resume`:**
   Все тесты цикла код-ревью в `tests/test_task_worker.py` полностью мокируют метод `_run_review_cycle`. Отсутствуют интеграционные тесты с реальным вызовом CLI `pi_runner resume`, что привело к пропуску ошибки обязательного параметра CLI.
2. **Проверка границ разрешений токенов исполнения:**
   В `tests/test_permissions.py:90` проверка `test_scoped_grant_enforces_target_resource` была построена некорректно: вместо вызова боевой функции `resources_for_execution` тест жестко прописывал ресурс в аргументах вызова. Это замаскировало уязвимость эскалации прав до `ADMIN_RESOURCE` (`*`).
3. **Асинхронный жизненный цикл операций в Telegram-тестах:**
   В `tests/telegram/test_analysis.py` использовался `StubClient`, возвращающий синхронный результат `succeeded`. Отсутствуют тесты на поведение клиента при реальном асинхронном жизненном цикле (`pending` -> повторный вызов -> показ готового отчета).
4. **Статус E2E сценария T06:**
   В соответствии с заданием, E2E реализация T06 ведется параллельно в отдельном воркспейсе. В текущей ветке сквозные тесты T06 отсутствуют.

---

## 3. Итоговый вердикт

**Решение:** **НЕ ПРИНЯТО (REJECTED / UNAPPROVED)**.
Разработка коммитов `fd62693..3af8653` содержит качественную проработку миграций и базовой архитектуры операций, но содержит критические дефекты P1 и P2, блокирующие корректное прохождение многораундового код-ревью и нарушающие безопасность прав доступа.

**Рекомендованные следующие шаги:**
1. Поручить исполнителю (Pi) исправление дефектов 1–4.
2. Добавить тесты без мокирования CLI для проверки команды `pi_runner resume`.
3. Исправить тест `test_scoped_grant_enforces_target_resource` на использование `resources_for_execution`.
4. Провести финальное ревью после интеграции сценария T06.

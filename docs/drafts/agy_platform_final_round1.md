# Финальный независимый отчёт ревью платформы Agent Manager

- **Объект проверки:** Репозиторий `agent-manager` на неизменяемом коммите `5087b6d` (HEAD, `master`, `platform/t08-final`).
- **Базовые точки отсчёта:** Исходный базовый коммит `fd62693`, предварительно отрецензированный коммит `3af8653`.
- **Фокус проверки:** Диапазон изменений `3af8653..5087b6d` (21 коммит, 35 изменённых файлов, +3850/-227 строк), а также смежные архитектурные границы `src/agent_manager`, `src/pi_runner`, миграции Alembic и тестовый контур.
- **Целевые контракты:** `docs/drafts/agy_platform_pre_review.md`, отчеты `T07F-result.md`, `T06R-result.md`, `T08V-result.md`, сценарий `docs/e2e-pirozhki-test-scenario.md`.
- **Статус / Вердикт:** **УСЛОВНО ОДОБРЕНО С ДЕФЕКТОМ P2 (CONDITIONALLY APPROVED WITH P2 DEFECT)**.
  Все 5 дефектов предварительного ревью (AGY pre-review) подтверждены как фактически исправленные без внесения регрессий и без применения антипаттернов (запрещенных строгих allowlist'ов символов или вечной идемпотентности). Основной детерминированный тестовый контур платформы полностью зелёный (463 passed, 2 skipped). Однако независимый запуск опционального контура реальной модели выявил воспроизводимый дефект уровня **P2** в регулярном выражении парсера замечаний ревьюера (`_FINDING_RE`), приводящий к сбою операции код-ревью при генерации замечаний в стандартном Markdown-формате с жирным начертанием (`**P1: ...**`).

---

## 1. Статус устранения замечаний предварительного ревью (Prior Findings Disposition)

| № | Дефект из `agy_platform_pre_review.md` | Исходный уровень | Статус проверки | Реализация и соответствие критериям |
|---|---|---|---|---|
| **1** | Фатальный сбой цикла доработки кода (Developer Resume Loop Broken без `--session`) | **P1** | **ПОДТВЕРЖДЕНО, ИСПРАВЛЕНО (CONFIRMED FIXED)** | В [`src/agent_manager/task_worker.py:436, 664-691`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/task_worker.py#L436) извлекается `session_id` из исходного запуска разработчика и передается аргументом `--session <id>` в `pi_runner resume`. При отсутствии сессии выполняется fallback на команду `run`. Добавлен детерминированный тест с запуском дочернего процесса `test_review_changes_requested_resumes_original_developer_session`. |
| **2** | Эскалация привилегий токенов исполнения до wildcard `*` через `MANAGEMENT_ACTIONS` | **P1** | **ПОДТВЕРЖДЕНО, ИСПРАВЛЕНО (CONFIRMED FIXED)** | В [`src/agent_manager/permissions.py:188-201`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/permissions.py#L188-L201) функция `resources_for_execution` гарантирует, что при указании `task_id` ресурс жестко ограничен `task:<id>` и `agent:<id>`. Wildcard `*` выдается исключительно сессиям без идентификатора задачи (`task_id is None`), таким как секретарь. Добавлены сквозные тесты изоляции в `test_isolation_permissions.py` и `test_operation_permissions.py`. |
| **3** | Бесконечный цикл pending и паразитные запуски модели в Telegram `/analyze` | **P2** | **ПОДТВЕРЖДЕНО, ИСПРАВЛЕНО (CONFIRMED FIXED)** | В [`src/agent_manager/telegram/handlers.py:256-285`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/telegram/handlers.py#L256-L285) хэндлер сначала запрашивает сохраненные отчеты через `list_analysis_reports`. При наличии отчета он сразу отображается без запуска LLM. При первом запуске или флаге `refresh` операция регистрируется с ключом идемпотентности, привязанным к ревизии задачи (`updated_at`). Антипаттерн вечной перманентной идемпотентности отвергнут. |
| **4** | Выход за пределы каталога базы знаний (Path Traversal в рекомендациях анализатора) | **P2** | **ПОДТВЕРЖДЕНО, ИСПРАВЛЕНО (CONFIRMED FIXED)** | В [`src/agent_manager/knowledge.py:34-53`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/knowledge.py#L34-L53) реализован общий резолвер `contained_target_path`, проверяющий `candidate.is_relative_to(base)`. Переходы `..`, абсолютные пути и внешние симлинки вызывают `KnowledgePathError`. При этом сохранена поддержка вложенных допустимых путей (например, `foo/bar`), без наложения искусственных ограничений на символы. |
| **5** | Нарушение схемы вывода инструмента `get_task_logs` в `policy.mjs` (возврат сырого Markdown) | **P3** | **ПОДТВЕРЖДЕНО, ИСПРАВЛЕНО (CONFIRMED FIXED)** | В [`src/pi_runner/policy.mjs:421-445`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/pi_runner/policy.mjs#L421-L445) инструмент переведен на вызов `backendRequest("GET", "/tasks/:id")` и возвращает структурированный JSON `{ status: "ok" | "failed", task_id, title, task_status, updated_at, summary, error, changed_files, review }`. Документация в `docs/secretary-tools.md` синхронизирована. |

---

## 2. Новые замечания по результатам финальной проверки (Actionable Findings)

### P2 Findings (Функциональные дефекты и хрупкость интерфейсов)

#### Дефект 1: Сбой разбора отчёта код-ревью при выделении замечаний жирным шрифтом Markdown в реальных моделях
- **Файл и строки:** [`src/agent_manager/review_executor.py:116-120`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/review_executor.py#L116-L120)
- **Следствие:**
  В коммите `4da2719` парсер `_FINDING_RE` был расширен для поддержки замечаний, обрамленных в обратные кавычки:
  ```python
  _FINDING_RE = re.compile(
      r"^\s*(?:[-*+]\s+)?`?\s*(P[1-3]|BLOCKER|CRITICAL|MAJOR|MINOR|INFO)"
      r"\s*[:\-]\s*(.+?)\s*`?\s*$",
      re.IGNORECASE,
  )
  ```
  Однако современные языковые модели (включая протестированную в E2E `deepseek-v4.1-flash` через `opencode-go`) стандартно форматируют замечания жирным шрифтом Markdown:
  ```markdown
  **P1: index.html:2 — кнопка фактически не становится зелёной.**
  ```
  или
  ```markdown
  - **P1: index.html:2 — ...**
  ```
  или
  ```markdown
  **P1**: index.html:2 — ...
  ```
  Текущее регулярное выражение ожидает начало строки (или маркер списка), за которым может следовать только символ кавычки `` `? ``, но **не допускает** маркеры жирного шрифта `**` или `__`. Также в классе разделителей `[:\-]` отсутствуют длинное тире (`—`, `\u2014`) и короткое тире (`–`, `\u2013`), если двоеточие заключено внутрь жирного блока.

  В результате функция `_findings_from_text` не находит ни одного замечания (`findings == []`).
  Далее в строках [`review_executor.py:390-393`](file:///home/itolstov/Projects/mtl/AgentManager/agent-manager/src/agent_manager/review_executor.py#L390-L393) срабатывает строгое правило:
  ```python
  if verdict == VERDICT_CHANGES_REQUESTED and not findings:
      raise ReviewFormatError(
          "review verdict changes_requested has no evidence-linked findings"
      )
  ```
  Это приводит к аварийному завершению операции ревью со статусом `invalid_report`. Реальные валидные замечания модели отклоняются как невалидный отчет.
- **Воспроизведение и доказательства:**
  1. Независимый запуск E2E теста с реальной моделью:
     ```bash
     AGENT_MANAGER_REAL_MODEL_E2E=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_e2e_real_model.py -q -o addopts='' --tb=short
     ```
     Результат выполнения:
     ```
     .F                                                                       [100%]
     =================================== FAILURES ===================================
     ______________________ test_real_model_pirozhki_lifecycle ______________________
     tests/test_e2e_real_model.py:294: in test_real_model_pirozhki_lifecycle
         assert review_outcome.status == "succeeded", (
     E   AssertionError: real review failed: OperationExecutionError: review failed: review verdict changes_requested has no evidence-linked findings [invalid_report] (diagnostics: .../round_1_failure.json)
     E   assert 'failed' == 'succeeded'
     ```
  2. Фактический отчёт модели из сессии (`summary.md`):
     ```markdown
     ### Замечания

     **P1: index.html:2 — кнопка фактически не становится зелёной.**
     Класс `class="primary"` добавлен, но в проекте нет ни одного определения `.primary`...

     ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ
     ```
     Прямой вызов `parse_review_report(summary_text)` завершается исключением:
     `ReviewFormatError: review verdict changes_requested has no evidence-linked findings`.
- **Исправление:**
  Сделать регулярное выражение `_FINDING_RE` устойчивым к Markdown-разметке жирного и наклонного шрифта, а также к вариациям тире:
  ```python
  _FINDING_RE = re.compile(
      r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?`?\s*(P[1-3]|BLOCKER|CRITICAL|MAJOR|MINOR|INFO)"
      r"(?:\*\*|__)?`?\s*[:\-—–]\s*(.+?)\s*`?(?:\*\*|__)?\s*$",
      re.IGNORECASE,
  )
  ```
  При извлечении тела замечания дополнительно очищать замыкающие символы Markdown:
  ```python
  body = match.group(2).strip().rstrip("*_` ")
  ```

---

## 3. Анализ архитектурных подсистем и полноты платформы

### 3.1. Разграничение прав и фабрика токенов (`permissions.py`)
- Фабрика токенов `resources_for_execution(actions, task_id=..., agent_id=...)` корректно изолирует контекст исполнения:
  - Любое исполнение, привязанное к задаче (`task_id` задан), получает доступ только к `task:<task_id>` и `agent:<agent_id>`.
  - Маршруты `PATCH /tasks/{id}` и `POST /operations` проверяют соответствие целевого ресурса через `ExecutionPermissions.authorize`.
  - Негативные тесты (`test_negative_scoped_api_permissions_on_populated_state`) доказывают невозможность чтения или модификации чужих задач через прямой сырой HTTP.
  - Административный токен и внутренние учетные данные никогда не сериализуются в JSON операций (поле `owner` сохраняет только `execution:agent_id|label` или `admin`).

### 3.2. Долговечные операции, идемпотентность и рестарт (`operations.py`, `operation_worker.py`)
- Повторные запросы `POST /operations` с одинаковым `idempotency_key` возвращают сохранённую операцию со статусом `200 OK` без повторного создания записи в БД.
- Попытка использовать существующий ключ идемпотентности с другим телом возвращает `409 Conflict`.
- При рестарте сервиса метод `reconcile_startup_orphans()` переводит зависшие операции в статус `interrupted`, исключая бесконечную блокировку воркеров.
- Все 9 миграций Alembic (`0001`–`0009`) корректно применяются как на чистой БД, так и последовательно с сохранением существующих задач. Режим SQLite WAL и прагмы `foreign_keys=ON`, `busy_timeout` настроены на каждом соединении.

### 3.3. Анализ логов, эволюция профилей и защита базы знаний (`analysis.py`, `knowledge.py`)
- Пакет свидетельств (`evidence`) анализатора строго ограничен целевой задачей и её операциями (тест `test_evidence_package_is_bounded_to_the_target_task`).
- Применение рекомендаций (`RecommendationService.apply`) проверяет совпадение `expected_revision` / `target_content_hash`. При обнаружении конкурентной модификации рекомендация переводится в статус `conflict` со статусом ответа `409 Conflict`.
- Одобренные изменения профилей сохраняются не только в таблице БД, но и экспортируются в `agents/<agent_id>.md` базы знаний (`KnowledgeBase.save_agent_profile`), что предотвращает откат изменений при рестарте и фоновом файловом синке.
- При удалении профиля (`DELETE /agents/{id}`) выставляется признак `archived: True`, удаляется файл из базы знаний и фиксируется git-коммит. Повторная инициализация базы знаний не воскрешает архивированных агентов.

### 3.4. Исполнение и парсинг вывода Runner (`runner_output.py`, `review_executor.py`)
- Добавлен надежный модуль `runner_output.py` (`extract_runner_result`), корректно разбирающий многострочный отформатированный JSON (`json.dumps(indent=2)`) и изолирующий сбалансированные JSON-объекты от случайного префиксного вывода.
- Исполнитель ревью `ReviewExecutor` гарантирует запуск дочернего процесса в изолированном каталоге с правами только для чтения (`read`, `bash`), передавая handbook discovery и сохраняя артефакты `round_<n>.json` и `round_<n>.md`.

---

## 4. Сводка верификации и результатов тестирования

1. **Полный стандартный тестовый набор (Default Suite):**
   - **Команда:** `PYTHONPATH=src .venv/bin/python -m pytest -o addopts='' -q --tb=short`
   - **Результат:** `463 passed, 2 skipped, 2 warnings in 50.51s`
   - **Код возврата:** `0`
   - Предупреждения: устаревание вызова `starlette.testclient` с `httpx` и валидация alias в Pydantic Field.
   - Пропущенные тесты (2 skipped): опциональные тесты реальной модели без переменной окружения `AGENT_MANAGER_REAL_MODEL_E2E=1`.

2. **Детерминированный сквозной E2E Pirozhki:**
   - **Команда:** `PYTHONPATH=src .venv/bin/python -m pytest tests/test_e2e_pirozhki.py -v -o addopts=''`
   - **Результат:** `4 passed in 5.72s`
   - **Код возврата:** `0`
   - Проверены: полный жизненный цикл создания агента, выполнение задачи разработчиком, автоматическое код-ревью, анализ сессии, утверждение рекомендаций, фиксация в git, архивация агента, рестарт с сохранением идемпотентности, строгое отклонение ревью без вердикта и негативные права доступа.

3. **Синтаксическая проверка JavaScript и Node.js контрактов:**
   - **Команда:** `node --check tests/e2e_pirozhki_tools.mjs && node --check tests/platform_tools_contract.mjs && node --check tests/policy_tool_names.mjs && node --check tests/policy_deferred_tools.mjs && node --check src/pi_runner/policy.mjs`
   - **Результат:** Все проверки завершились успешно.
   - **Код возврата:** `0`

4. **Проверка git diff:**
   - **Команда:** `git diff --check 3af8653..HEAD`
   - **Результат:** Нет пробельных нарушений или конфликтов.
   - **Код возврата:** `0`

5. **Опциональный тестовый контур с реальной моделью:**
   - **Команда:** `AGENT_MANAGER_REAL_MODEL_E2E=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_e2e_real_model.py -q -o addopts='' --tb=short`
   - **Результат:** `1 failed, 1 passed in 44.13s`
   - **Код возврата:** `1`
   - Причина сбоя: дефект P2 в `_FINDING_RE` (`review verdict changes_requested has no evidence-linked findings`).

---

## 5. Итоговый вердикт

**Решение:** **УСЛОВНО ОДОБРЕНО С ДЕФЕКТОМ P2 (CONDITIONALLY APPROVED WITH P2 DEFECT)**.

1. Все 5 критических замечаний предварительного ревью устранены качественно, архитектурно чисто и подтверждены независимыми изолированными тестами.
2. Детерминированный тестовый контур платформы полностью функционален (463 теста проходят).
3. До ввода в продуктивную эксплуатацию контура с реальными LLM-моделями требуется применить исправление регулярного выражения `_FINDING_RE` в `src/agent_manager/review_executor.py:116-120` для поддержки выделения замечаний жирным шрифтом Markdown.

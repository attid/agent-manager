# Развертывание и эксплуатация (Production Deployment)

Agent Manager развернут как пользовательский сервис systemd (`agent-manager.service`) в домашней директории пользователя.

---

## 1. Структура окружения и каталогов

- **Рабочая директория разработки**: `/home/itolstov/Projects/mtl/AgentManager/agent-manager`
- **Продакшн директория**: `/home/itolstov/.local/lib/agent-manager` (или `/home/itolstov/.agent-manager`)
- **Конфигурация окружения**: `/home/itolstov/.config/agent-manager/agent-manager.env`
  *(Внимание: никогда не редактировать и не коммитить этот файл в репозиторий!)*
- **База данных SQLite**: `/home/itolstov/.local/share/agent-manager/agent_manager.db`
- **База знаний**: `/home/itolstov/.local/share/agent-manager/knowledge`
- **Скилы агентов**: `/home/itolstov/.local/share/agent-manager/skills`

---

## 2. Скрипт безопасного деплоя в Production

Для обновления продакшн-окружения используется скрипт:
```bash
python agent-manager/scripts/deploy_production.py
```

Что делает скрипт:
1. Запускает полный набор тестов (`pytest agent-manager/tests/` и `pytest pi-runner/tests/`).
2. Копирует актуальный код `agent-manager` и `pi-runner` в продакшн-директорию с исключением временных файлов, кэшей и баз данных.
3. Синхронизирует виртуальное окружение через `uv sync --no-install-project`.
4. Применяет миграции Alembic (`alembic upgrade head`) к продакшн-базе.
5. Перезапускает пользовательский systemd сервис:
   ```bash
   systemctl --user restart agent-manager.service
   ```
6. Проверяет доступность эндпоинта здоровья:
   ```bash
   curl http://127.0.0.1:18767/health
   ```

---

## 3. Команды управления сервисом

- **Проверка статуса**:
  ```bash
  systemctl --user status agent-manager.service
  ```
- **Просмотр логов в реальном времени**:
  ```bash
  journalctl --user -u agent-manager.service -f -n 100
  ```
- **Перезапуск**:
  ```bash
  systemctl --user restart agent-manager.service
  ```
- **Остановка**:
  ```bash
  systemctl --user stop agent-manager.service
  ```

# Baseline Behavior: HunttechProtocols Bot (tg_conspect_bot)

> Зафиксировано перед рефакторингом подключения общей библиотеки.
> Дата: 2026-07-12
> Git commit: fed80001c641af20dd11136ea825f0a02180ab3f
> Ветка: refactor/use-common-library (создана от main)

## 1. Запуск

- **Точка входа:** `/root/tg_conspect_bot/bot.py`
- **Запуск:** `python3 bot.py`
- **Зависимости:** `requirements.txt` (aiogram>=3.0, httpx>=0.27, python-dotenv>=1.0, socksio, asyncpg>=0.29)
- **Конфигурация:** `.env` (TG_TOKEN обязателен, DB_HOST/DB_PASSWORD опциональны)

## 2. Команды и подкоманды

### Основные команды
| Команда | Назначение | Доступ |
|---------|-----------|--------|
| `/start` | Приветствие | Все |
| `/help` | Справка | Все |
| `/list` | Список непрочитанных конспектов | После настройки почты |
| `/list_all` | Все конспекты за неделю | После настройки почты |
| `/list new` | Только новые (не показанные) конспекты | После настройки почты |
| `/setup` | Настройка почты, AI, Wiki, БД | Все |
| `/setup email ...` | Изменить email | Все |
| `/setup imap ...` | Изменить IMAP-сервер | Все |
| `/setup login ...` | Изменить логин IMAP | Все |
| `/setup password` | Изменить пароль IMAP | Все |
| `/setup_ai` | Настройка AI-провайдера | Все |
| `/setup wiki` | Настройка Яндекс Wiki | Все |
| `/setup wiki test` | Тест Wiki | Все |
| `/setup_db` | Настройка PostgreSQL | Только админ (272980897) |
| `/cancel` | Отмена операции | Все |
| `/prompt` | Управление промптами | Все |
| `/add_prompt` | Добавить промпт | Все |
| `/edit_prompt` | Редактировать промпт | Все |
| `/text_prompt` | Показать текст промпта | Все |
| `/delete_prompt` | Удалить промпт | Все |
| `/wiki_test` | Тест Яндекс Wiki | Все |

### Административные команды
- `/setup_db` — только user_id=272980897
- `/setup db ...` — только админ

## 3. Help

- `/help` — показывает общий список команд с эмодзи
- `/help <command>` — детальная справка по команде
- Отображается через `_format_prompt_list()` и `render_help_command()`

## 4. Боковое меню Telegram

Ботовое меню не устанавливается через BotCommandScope — используется только текстовый help.

## 5. Права

- **admin:** user_id=272980897 (AlekseyAnanyev)
- **Обычный пользователь:** все остальные
- `/setup_db` проверяет `ADMIN_USER_ID = 272980897`
- Доступ по IMAP проверяется наличием конфигурации пользователя

## 6. AI-функции

### call_ai(user_id, system_prompt, user_text) -> str
- Вызывает OpenAI-compatible API
- Параметры: endpoint, api_key, model из users.json
- timeout=120 секунд
- temperature=0.7
- Возвращает строку (содержимое ответа или ❌ ошибка)
- Ошибки: таймаут, 401, 404, ConnectError, общая ошибка

### _test_ai_connection(endpoint, api_key, model) -> str
- Проверяет подключение с max_tokens=10
- timeout=15 секунд

### _strip_json_markdown(text) -> str
- Удаляет Markdown-блоки из JSON

## 7. База данных (db.py)

- **СУБД:** PostgreSQL через asyncpg
- **Пул:** глобальный `DB_POOL: Optional[asyncpg.Pool]`
- **Включение:** когда DB_HOST и DB_PASSWORD непустые
- **Таблицы:** `meetings`, `summary_log`
- **Функции:** `init_db_pool()`, `close_db_pool()`, `ensure_tables()`, `save_meeting()`, `save_summary()`, `get_meeting_by_msg_id()`, `get_recent_meetings()`, `get_summaries_for_meeting()`, `get_stats()`
- **ADMIN_USER_ID:** 272980897

## 8. Конфигурация

- **Файл:** `.env`
- **Переменные:** TG_TOKEN, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
- **Загрузка:** `load_dotenv()` в bot.py и db.py
- **Хранение пользователей:** `users.json` (JSON-файл)
- **Хранение промптов:** `prompts.json` (JSON-файл)

## 9. FSM-состояния

| Класс | Назначение |
|-------|-----------|
| `SetupState` | Настройка IMAP (email → server → login → password) |
| `AiSetupState` | Настройка AI (provider → api_key → model) |
| `WikiSetupState` | Настройка Wiki (api_key) |
| `DbSetupState` | Настройка PostgreSQL (host → port → name → user → password) |
| `SetupSingleField` | Изменение одного поля почты |
| `AddPromptState` | Добавление промпта (topic → text) |
| `EditPromptState` | Редактирование промпта (topic → text) |
| `DeletePromptState` | Удаление промпта (topic) |
| `GetPromptState` | Просмотр промпта (topic) |
| `AskAddFirstPrompt` | Онбординг первого промпта |

## 10. Callback-кнопки

| Pattern | Назначение |
|---------|-----------|
| `prompt:add/edit/text/delete` | Управление промптами |
| `first_prompt:yes/no` | Онбординг промптов |
| `summary:IDX:PROMPT` | Генерация саммари |
| `choose_prompt:IDX` | Выбор промпта для конспекта |
| `publish_wiki:IDX:PROMPT` | Публикация в Wiki |
| `ai_provider:KEY` | Выбор AI-провайдера |

## 11. Логирование

- `logging.basicConfig(level=INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")`
- Логгер: `logger = logging.getLogger("bot")`
- В db.py: `logger = logging.getLogger("db")`
- API-ключи НЕ маскируются

## 12. Временные файлы

- `tempfile.mkdtemp()` + `shutil.rmtree()` в `_extract_text_from_file()`

## 13. Docker

- Нет Dockerfile

## 14. Тесты

- Нет существующих unit-тестов
- Только ручное тестирование

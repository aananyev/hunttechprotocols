# 🤖 HuntTech Protocols Bot

Telegram-бот для извлечения конспектов встреч из почты и генерации AI-саммари.

Бот: [@hunttech_protocols_bot](https://t.me/hunttech_protocols_bot)

## Возможности

### 📬 Почта (IMAP)
- Подключение к Yandex, Mail.ru и любому IMAP-серверу
- Поиск **непрочитанных** писем с темой «Конспект встречи»
- Флаг UNSEEN **не снимается** — письма остаются непрочитанными
- `/list` — только непрочитанные
- `/list_all` — все за последние 7 дней

### 🤖 AI-саммари
- Поддержка OpenRouter, OpenAI, DeepSeek и любых OpenAI-совместимых API
- Привязка промптов к темам конспектов по началу названия
- Кнопка 🟢 **Саммари** под каждым конспектом — одноклик отчёт по вашему промпту
- Настройка: `/setup_ai`

### 📝 Управление промптами
- `/add_prompt`, `/edit_prompt`, `/text_prompt`, `/delete_prompt`
- Инлайн-кнопки для всех действий
- Подкоманды: `/prompt add`, `/prompt edit` и т.д.
- Авто-показ списка после каждого изменения

### 🔧 Настройка
- `/setup` — 4 шага: Email → IMAP-сервер → Логин → Пароль приложения
- `/init` — сброс и настройка заново
- `/setup_ai` — выбор провайдера и модели
- `/help` — полная справка

## Технологии

- Python + aiogram 3.x
- IMAP (Yandex, любой)
- OpenAI-compatible API (OpenRouter, DeepSeek, OpenAI)
- JSON-хранилище (многопользовательское)
- python-dotenv (токен в .env, не в коде)

## Запуск

```bash
pip install -r requirements.txt
cp .env.example .env
# Вписать TG_TOKEN в .env (от @BotFather)
python3 bot.py
```

## Безопасность

- Токен Telegram — в `.env`, исключён из git
- Пароли приложений — в `users.json`, исключены из git
- AI-ключи — в `users.json`, исключены из git
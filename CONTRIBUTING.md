# СИГМА — репозиторий проекта

Два Telegram-бота для такси/доставки: клиентский бот и бот-диспетчер для чата водителей.
Подробности — в [README.md](README.md), деплой — в [DEPLOY.md](DEPLOY.md).

## Быстрый старт для разработчика

```bash
git clone https://gitlab.com/VPNmAsteRR/sigma-taxi-bots.git
cd sigma-taxi-bots

cp .env.example .env
# заполнить токены ботов, GROUP_CHAT_ID и ADMIN_CODE

python3 -m unittest discover -s tests -v   # 30 тестов
python3 bot.py
```

Нужен только Python 3.10+. Зависимостей нет.

## Структура

```
bot.py     оба бота, регистрация водителя, админка, автоотмена
core.py    карточки заказов, кнопки, ActionGuard, статистика
store.py   SQLite: заказы, водители, события, сессии
tgapi.py   минимальный клиент Telegram Bot API
tests/     тесты: логика, сценарии ботов, защита от двойных кликов
```

## Правила работы

- **Секреты только в `.env`**, он в `.gitignore`. Никогда не коммить токены,
  пароли и адреса серверов.
- Перед пушем: `python3 -m unittest discover -s tests -v`
- Ветки: `main` — рабочая, изменения через merge request.

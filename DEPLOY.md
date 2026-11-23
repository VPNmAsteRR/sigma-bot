# Деплой СИГМЫ на сервер

Инструкция для развёртывания на Ubuntu 22.04 / Python 3.10.
Адрес сервера и пароли в репозиторий не кладём — они выдаются отдельно.

## Что где лежит

| Что | Где |
| --- | --- |
| Код | `/opt/sigma-bot/` |
| Конфиг | `/opt/sigma-bot/.env` (права 600) |
| База | `/opt/sigma-bot/data/sigma.db` |
| Служба | `sigma-bot.service` (systemd, автозапуск) |
| Лог | `/var/log/sigma-bot.log` (ротация 14 дней) |
| Часовой пояс | Europe/Moscow |

## Управление

```bash
ssh root@<SERVER_IP>

systemctl status sigma-bot     # состояние
systemctl restart sigma-bot    # перезапуск
systemctl stop sigma-bot       # остановка
tail -f /var/log/sigma-bot.log # логи в реальном времени
```

## Установка с нуля

```bash
# 1. Код
mkdir -p /opt/sigma-bot
cd /opt/sigma-bot
git clone <REPO_URL> .   # или скопировать файлы вручную

# 2. Конфиг
cp .env.example .env
nano .env                # токены, GROUP_CHAT_ID, ADMIN_CODE
chmod 600 .env
mkdir -p data

# 3. Служба
cat > /etc/systemd/system/sigma-bot.service <<'EOF'
[Unit]
Description=Sigma taxi bots
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/sigma-bot
EnvironmentFile=/opt/sigma-bot/.env
ExecStart=/usr/bin/python3 /opt/sigma-bot/bot.py
Restart=always
RestartSec=5
User=root
StandardOutput=append:/var/log/sigma-bot.log
StandardError=append:/var/log/sigma-bot.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now sigma-bot
systemctl is-active sigma-bot
```

## Добавление бота в чат заказов

Бот-таксист не может сам вступить в группу — его нужно добавить вручную.

1. Открой группу заказов (например `@TaxiSigmaChat`).
2. Название группы → **Участники** → **Добавить участника**.
3. Найди **@TaxiSigmaJobBot** → добавь.
4. Нажми на бота в списке → **Сделать администратором** →
   галочки «Отправка сообщений» и «Закрепление» → Сохранить.

Почему администратором: обычные боты в группах не видят сообщения из-за
privacy mode. Права админа (или `/setprivacy` → Disable у @BotFather) это
снимают — тогда работают `/id`, `/forward` и команды в чате.

Проверка:

```bash
# подставь токен бота-таксиста из .env
curl -s "https://api.telegram.org/bot<GROUP_BOT_TOKEN>/getChatMember?chat_id=<GROUP_CHAT_ID>&user_id=<BOT_ID>"
```

Должно быть `"status":"member"` или `"administrator"`.

Пока бот не в группе, заказы не теряются: они ждут в базе со статусом `new`,
а бот каждые 15 секунд пытается дослать их (`retry_unpublished`).

## Проверка работы

1. Напиши клиентскому боту → `/start` → «Заказать такси».
2. Введи откуда, куда, комментарий (`-`), телефон (`-`).
3. Нажми «✅ Отправить».
4. В группе появится карточка с кнопкой «✅ Принять заказ».
5. Водитель сначала регистрируется в личке бота-таксиста: `/start` → ФИО →
   марка машины → цвет → гос. номер → телефон.
6. Нажми «Принять заказ» — карточка обновится, клиенту придёт ФИО, машина и телефон.

## Админка

`/admin` в личке любого бота, код — из `ADMIN_CODE` в `.env`.

Сразу после первого входа смени код в `/opt/sigma-bot/.env` и перезапусти:

```bash
nano /opt/sigma-bot/.env
systemctl restart sigma-bot
```

## Обновление кода

```bash
ssh root@<SERVER_IP>
cd /opt/sigma-bot
git pull
systemctl restart sigma-bot
```

Если код копируется файлами:

```bash
scp bot.py core.py store.py tgapi.py main.py root@<SERVER_IP>:/opt/sigma-bot/
ssh root@<SERVER_IP> "systemctl restart sigma-bot"
```

## Резервная копия базы

```bash
ssh root@<SERVER_IP> "sqlite3 /opt/sigma-bot/data/sigma.db '.backup /root/sigma-\$(date +%F).db'"
```

## Безопасность

- `.env` не коммитим, права `600`.
- Токены ботов и root-пароли передаём только приватно (не в чатах и не в git).
- При утечке токена: @BotFather → `/mybots` → бот → **API Token** → **Revoke**.
- Вход по SSH лучше перевести на ключи:

```bash
ssh-copy-id root@<SERVER_IP>
# в /etc/ssh/sshd_config: PasswordAuthentication no
systemctl restart ssh
```

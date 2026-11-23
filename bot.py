#!/usr/bin/env python3
"""СИГМА — два Telegram-бота для заказов.

1. Бот-заказчик (@..._client): клиент пишет откуда/куда, бот публикует заказ
   в чат водителей и присылает ФИО/телефон того, кто принял.
2. Бот-диспетчер (@..._group): сидит в чате водителей, публикует карточки
   заказов с кнопкой «Принять», обновляет статус и данные принявшего.

Только стандартная библиотека Python + SQLite. Один процесс, два потока.
"""

import argparse
import hmac
import logging
import os
import re
import signal
import threading
import time

from core import (
    EMPTY_KEYBOARD,
    SigmaService,
    admin_drivers_text,
    admin_orders_text,
    admin_panel_keyboard,
    admin_panel_text,
    client_cancelled_text,
    client_keyboard,
    client_search_text,
    client_status_text,
    courier_profile_text,
    courier_stats_text,
    esc,
    public_id,
)
from filters import classify, dedupe_key
from store import Store
from tgapi import BotAPI, TelegramError

log = logging.getLogger("sigma")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------- helpers
def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def button(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def inline_keyboard(rows):
    return {"inline_keyboard": rows}


def force_reply(placeholder):
    return {"force_reply": True, "input_field_placeholder": placeholder}


def remove_keyboard():
    return {"remove_keyboard": True}


def display_name(user):
    if not user:
        return None
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
    return name or user.get("username") or str(user.get("id"))


def clean_address(text):
    value = " ".join((text or "").split())
    if len(value) < 2 or len(value) > 200:
        return None
    return value


def clean_text(text, minimum, maximum):
    value = " ".join((text or "").split())
    if len(value) < minimum or len(value) > maximum:
        return None
    return value


def normalize_phone(text):
    raw = " ".join((text or "").split())
    lowered = raw.lower()
    if not raw or lowered in {"-", "нет"} or "пропустить" in lowered or "skip" in lowered:
        return ""
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        return None
    return f"+{digits}"


def is_private(msg):
    return (msg.get("chat") or {}).get("type") == "private"


def is_group(msg):
    return (msg.get("chat") or {}).get("type") in {"group", "supergroup"}


def url_button(text, url):
    return {"text": text, "url": url}


class BotBase:
    name = "bot"
    commands = []

    def __init__(self, api, store, service, stop_event, bot_username="", admin_code=""):
        self.api = api
        self.store = store
        self.service = service
        self.stop_event = stop_event
        self.bot_username = bot_username or ""
        self.admin_code = admin_code or ""
        # автопересылка заказов (настраивается в main())
        self.forward_min_score = 2
        self.forward_min_length = 15
        self.forward_dedupe_ttl = 900
        self.forward_as_copy = True
        self.bot_id = None
        # куда слать заказы (числовые id) и оформление
        self.forward_to = []
        self.forward_header = ""
        self.forward_with_header = False
        self.forward_extra_triggers = []
        self.forward_extra_negatives = []

    # ------------------------------------------------------------------- loop
    def run(self):
        me = self.api.get_me()
        self.bot_id = me.get("id")
        self.bot_username = me.get("username") or self.bot_username
        log.info("%s started: @%s id=%s", self.name, self.bot_username, me.get("id"))
        if self.commands:
            try:
                self.api.set_my_commands(self.commands)
            except TelegramError as exc:
                log.warning("%s setMyCommands: %s", self.name, exc)

        offset_key = f"offset:{self.name}"
        offset_raw = self.store.meta_get(offset_key)
        offset = int(offset_raw) if offset_raw else None

        while not self.stop_event.is_set():
            try:
                updates = self.api.get_updates(
                    offset, timeout=25,
                    allowed_updates=[
                        "message",
                        "callback_query",
                        "my_chat_member",
                        "channel_post",
                    ],
                )
            except TelegramError as exc:
                if exc.code == 409:
                    log.error("%s: этот токен уже опрашивает другой процесс", self.name)
                    time.sleep(10)
                else:
                    log.warning("%s getUpdates: %s", self.name, exc)
                    time.sleep(3)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("%s update %s failed", self.name, update.get("update_id"))
            if updates:
                self.store.meta_set(offset_key, offset)
            self.tick()

        log.info("%s stopped", self.name)

    def tick(self):
        """Периодическая задача после каждого цикла опроса."""

    def handle_update(self, update):
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        if "my_chat_member" in update:
            self.handle_my_chat_member(update["my_chat_member"])
            return
        if "channel_post" in update:
            self.remember_chat((update["channel_post"].get("chat") or {}))
            return
        msg = update.get("message")
        if msg:
            self.remember_chat(msg.get("chat") or {})
            self.handle_message(msg)

    def remember_chat(self, chat):
        """Запомнить группу/канал: потом пригодится для рассылки (--all-seen)."""
        if not chat or not chat.get("id"):
            return
        if chat.get("type") not in {"group", "supergroup", "channel"}:
            return
        try:
            self.store.remember_chat(chat["id"], chat.get("title"), chat.get("type"))
        except Exception:  # noqa: BLE001
            log.exception("remember_chat %s failed", chat.get("id"))

    def handle_my_chat_member(self, update):
        """Бота добавили в группу или выдали ему права — знакомимся."""
        chat = update.get("chat") or {}
        status = ((update.get("new_chat_member") or {}).get("status") or "").lower()
        if chat.get("type") not in {"group", "supergroup", "channel"}:
            return
        if status in {"left", "kicked"}:
            log.info("бота убрали из чата %s (%s)", chat.get("id"), chat.get("title"))
            return
        self.remember_chat(chat)
        log.info(
            "бот добавлен в чат %s «%s» status=%s",
            chat.get("id"), chat.get("title"), status,
        )
        if status in {"administrator", "creator"}:
            text = (
                "✅ <b>На связи.</b>\n\n"
                f"Чат: <code>{chat.get('id')}</code>\n\n"
                "Могу публиковать здесь заказы и рекламу с кнопкой "
                "«Заказать такси».\n"
                "Команда <code>/post</code> — опубликовать пост с кнопкой."
            )
        else:
            text = (
                "👋 <b>Готов к работе.</b>\n\n"
                "Чтобы публиковать посты с кнопкой, дайте мне права "
                "администратора (или разрешите писать сообщения).\n"
                f"ID чата: <code>{chat.get('id')}</code>"
            )
        try:
            self.api.send_message(chat["id"], text)
        except TelegramError as exc:
            log.warning("приветствие в %s не ушло: %s", chat.get("id"), exc)

    # ------------------------------------------------------- group messages
    def handle_group(self, msg):
        """Команды в группе + автопересылка заказов из чатов-источников."""
        text = msg.get("text") or ""
        if text.startswith("/"):
            cmd, _, args = text.partition(" ")
            cmd = cmd.lstrip("/").split("@")[0].lower()
            handler = getattr(self, f"cmd_{cmd}", None)
            if handler:
                handler(msg, args.strip())
                return
        self.autoforward(msg)

    # ------------------------------------------------------------- autoforward
    def autoforward(self, msg):
        """Заказ из чата-источника → отдельный чат/личка (не в этот же чат)."""
        chat_id = msg["chat"]["id"]
        if not self.store.is_forward_source(chat_id):
            return

        # свои и ботовые сообщения не пересылаем (нет петли)
        sender = msg.get("from") or {}
        if self.bot_id and sender.get("id") == self.bot_id:
            return
        if sender.get("is_bot"):
            return

        message_id = msg.get("message_id")
        if not message_id:
            return
        if self.store.already_forwarded(chat_id, message_id):
            return

        text = msg.get("text") or msg.get("caption") or ""
        if not text.strip():
            return

        verdict = classify(
            text,
            self.forward_min_score,
            self.forward_min_length,
            extra_triggers=self.forward_extra_triggers,
            extra_negatives=self.forward_extra_negatives,
        )
        if not verdict.is_order:
            log.debug("autoforward skip (%s): %s", verdict.explain(), text[:80])
            return

        key = dedupe_key(text)
        if self.store.is_duplicate_text(key, self.forward_dedupe_ttl):
            log.info("autoforward: дубль из %s пропущен", chat_id)
            self.store.mark_forwarded(chat_id, message_id, key)
            return

        targets = self._forward_targets()
        if not targets:
            log.warning("autoforward: нет чата-приёмника (настройте FORWARD_TO или /target)")
            return

        source_title = (msg.get("chat") or {}).get("title") or str(chat_id)
        sent = 0
        for target in targets:
            if target == chat_id:
                log.warning(
                    "autoforward: приёмник совпадает с источником %s — пропускаю", chat_id
                )
                continue
            try:
                if self.forward_with_header:
                    header = self.forward_header.format(source=esc(source_title))
                    self.service.group_api.send_message(target, header)
                if self.forward_as_copy:
                    self.service.group_api.copy_message(target, chat_id, message_id)
                else:
                    self.service.group_api.forward_message(target, chat_id, message_id)
                sent += 1
            except TelegramError as exc:
                log.error("autoforward %s→%s failed: %s", chat_id, target, exc)

        if not sent:
            return

        self.store.mark_forwarded(chat_id, message_id, key)
        log.info(
            "autoforward: %s → %s (%s) %s",
            chat_id, targets, verdict.explain(), text[:60],
        )

    def _forward_targets(self):
        """Куда слать заказы: FORWARD_TO из .env + добавленные командами."""
        from_env = list(self.forward_to or [])
        from_db = [r["chat_id"] for r in self.store.forward_targets(only_enabled=True)]
        return list(dict.fromkeys(from_env + from_db))

    # ------------------------------------------------------- source management
    def cmd_source(self, msg, args):
        """Добавить текущий чат в источники: /source"""
        chat_id = msg["chat"]["id"]
        if is_private(msg):
            self.send(chat_id, "Команду <code>/source</code> пишите в самом чате-источнике.")
            return
        if not self._can_manage(chat_id, msg.get("from") or {}):
            self.send(chat_id, "⛔ Добавлять источники может только администратор чата.")
            return
        if self.store.is_forward_source(chat_id):
            self.send(chat_id, "ℹ️ Этот чат уже в списке источников.")
            return
        title = (msg.get("chat") or {}).get("title")
        self.store.add_forward_source(chat_id, title)
        self.send(
            chat_id,
            f"✅ Чат добавлен в источники.\nID: <code>{chat_id}</code>\n\n"
            f"Заказы отсюда будут уходить в чат для заказов.",
        )

    def _can_manage(self, chat_id, user):
        """Админ чата или админ бота."""
        if not user.get("id"):
            return False
        if self.store.is_admin(user["id"]):
            return True
        try:
            member = self.api.get_chat_member(chat_id, user["id"])
        except TelegramError as exc:
            log.warning("getChatMember %s/%s: %s", chat_id, user.get("id"), exc)
            return False
        return (member or {}).get("status") in {"creator", "administrator"}

    def cmd_unsource(self, msg, args):
        chat_id = msg["chat"]["id"]
        if not is_private(msg) and not self._can_manage(chat_id, msg.get("from") or {}):
            self.send(chat_id, "⛔ Управлять источниками может только администратор чата.")
            return
        if self.store.remove_forward_source(chat_id):
            self.send(chat_id, "🗑 Чат убран из источников.")
        else:
            self.send(chat_id, "Этот чат не был в источниках.")

    def cmd_sources(self, msg, args):
        chat_id = msg["chat"]["id"]
        rows = self.store.forward_sources()
        if not rows:
            self.send(
                chat_id,
                "Источников нет.\n\nДобавьте бота в чат с заказами и напишите там "
                "<code>/source</code>.",
            )
            return
        lines = ["📡 <b>Чаты-источники</b>\n"]
        for row in rows:
            lines.append(
                f"• {esc(row.get('title') or 'без названия')} — "
                f"<code>{row['chat_id']}</code>"
            )
        self.send(chat_id, "\n".join(lines))

    # ------------------------------------------------------ target management
    def cmd_target(self, msg, args):
        """Куда присылать заказы: /target — текущий чат/личка."""
        chat_id = msg["chat"]["id"]
        title = (msg.get("chat") or {}).get("title") or (
            "личка" if is_private(msg) else str(chat_id)
        )
        if self.store.is_forward_target(chat_id):
            self.send(chat_id, "ℹ️ Этот чат уже приёмник заказов.")
            return
        self.store.add_forward_target(chat_id, title)
        self.send(
            chat_id,
            f"✅ Заказы теперь будут приходить сюда.\nID: <code>{chat_id}</code>\n\n"
            f"Чтобы проверить — напишите заказ в чате-источнике.",
        )

    def cmd_targets(self, msg, args):
        chat_id = msg["chat"]["id"]
        rows = self.store.forward_targets()
        lines = ["📥 <b>Куда приходят заказы</b>\n"]
        for row in rows:
            lines.append(
                f"• {esc(row.get('title') or 'без названия')} — "
                f"<code>{row['chat_id']}</code>"
            )
        if self.forward_to:
            lines.append("\nИз настроек (<code>FORWARD_TO</code>):")
            for t in self.forward_to:
                lines.append(f"• <code>{t}</code>")
        if len(lines) == 1:
            lines.append(
                "Пока пусто. Напишите <code>/target</code> в нужном чате или в личке бота."
            )
        self.send(chat_id, "\n".join(lines))

    def cmd_untarget(self, msg, args):
        chat_id = msg["chat"]["id"]
        if self.store.remove_forward_target(chat_id):
            self.send(chat_id, "🗑 Этот чат больше не принимает заказы.")
        else:
            self.send(chat_id, "Этот чат не был приёмником заказов.")

    # --------------------------------------------------------------- API sugar
    def send(self, chat_id, text, reply_markup=None):
        try:
            return self.api.send_message(chat_id, text, reply_markup=reply_markup)
        except TelegramError as exc:
            log.warning("%s send to %s failed: %s", self.name, chat_id, exc)
            return None

    def edit(self, chat_id, message_id, text, reply_markup=None):
        try:
            self.api.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)
            return True
        except TelegramError as exc:
            if "message is not modified" in str(exc).lower():
                return True
            log.warning("%s edit %s/%s failed: %s", self.name, chat_id, message_id, exc)
            return False

    def answer(self, callback, text=None, alert=False):
        try:
            self.api.answer_callback_query(callback["id"], text=text, show_alert=alert)
        except TelegramError as exc:
            log.debug("%s answerCallbackQuery failed: %s", self.name, exc)

    # ----------------------------------------------------------------- admins
    def begin_admin_login(self, chat_id):
        if not self.admin_code:
            self.send(
                chat_id,
                "Админка не настроена. Добавьте <code>ADMIN_CODE</code> в .env.",
            )
            return
        self.store.set_session(self.name, chat_id, "admin_code", {})
        self.send(chat_id, "🔐 <b>Админка СИГМА</b>\n\nВведите код администратора.")

    def admin_code_step(self, chat_id, text):
        code = (text or "").strip()
        if self.admin_code and hmac.compare_digest(code, self.admin_code):
            self.store.add_admin(chat_id)
            self.store.clear_session(self.name, chat_id)
            self.show_admin_panel(chat_id)
            return
        self.store.clear_session(self.name, chat_id)
        self.send(chat_id, "❌ Неверный код.")

    def show_admin_panel(self, chat_id, edit_message_id=None):
        if not self.store.is_admin(chat_id):
            self.begin_admin_login(chat_id)
            return
        stats = self.store.admin_stats()
        text = admin_panel_text(stats)
        keyboard = admin_panel_keyboard()
        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, keyboard)
        else:
            self.send(chat_id, text, keyboard)

    def handle_admin_callback(self, callback, data, message_id):
        chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
        if not chat_id or not self.store.is_admin(chat_id):
            self.answer(callback, "Только для админа", alert=True)
            return True
        if data == "a:panel":
            self.answer(callback)
            self.show_admin_panel(chat_id, message_id)
            return True
        if data == "a:drivers":
            self.answer(callback)
            text = admin_drivers_text(self.store.couriers_with_stats())
            keyboard = inline_keyboard([[button("⬅️ Назад", "a:panel")]])
            if message_id:
                self.edit(chat_id, message_id, text, keyboard)
            else:
                self.send(chat_id, text, keyboard)
            return True
        if data == "a:orders":
            self.answer(callback)
            text = admin_orders_text(self.store.recent_orders(10))
            keyboard = inline_keyboard([[button("⬅️ Назад", "a:panel")]])
            if message_id:
                self.edit(chat_id, message_id, text, keyboard)
            else:
                self.send(chat_id, text, keyboard)
            return True
        return False

    def cmd_admin(self, msg, args):
        if not is_private(msg):
            self.send(msg["chat"]["id"], "Админка открывается в личке бота.")
            return
        chat_id = msg["chat"]["id"]
        if self.store.is_admin(chat_id):
            self.show_admin_panel(chat_id)
        else:
            self.begin_admin_login(chat_id)


# ================================================================ client bot
class ClientBot(BotBase):
    name = "client"
    commands = [
        ("start", "Заказать такси"),
        ("status", "Мой заказ"),
        ("cancel", "Отменить заказ"),
        ("admin", "Админка"),
        ("id", "Мой Telegram ID"),
        ("source", "Добавить этот чат в источники"),
        ("unsource", "Убрать этот чат из источников"),
        ("sources", "Список чатов-источников"),
        ("post", "Опубликовать пост с кнопкой"),
        ("text", "Текст поста"),
        ("button", "Кнопка поста"),
    ]

    def handle_message(self, msg):
        if not is_private(msg):
            # в группе клиентский бот тоже умеет ловить заказы из источников
            self.handle_group(msg)
            return
        chat_id = msg["chat"]["id"]
        user = msg.get("from") or {}
        self.store.ensure_client(chat_id, display_name(user), user.get("username"))

        text = msg.get("text") or ""
        if text.startswith("/"):
            cmd, _, args = text.partition(" ")
            cmd = cmd.lstrip("/").split("@")[0].lower()
            handler = getattr(self, f"cmd_{cmd}", None)
            if handler:
                handler(msg, args.strip())
                return

        session = self.store.get_session(self.name, chat_id)
        state = session["state"] if session else None

        if state == "admin_code":
            self.admin_code_step(chat_id, text)
            return

        if state in {"from", "to", "comment", "phone"}:
            self.form_step(chat_id, state, text, session)
            return

        if state == "preview":
            self.send(
                chat_id,
                "Осталось отправить заказ. Нажмите «✅ Отправить» "
                "или «❌ Отмена» под карточкой.",
            )
            return

        self.send_main(chat_id)

    def form_step(self, chat_id, state, text, session):
        data = dict(session.get("data") or {})

        if state == "from":
            address = clean_address(text)
            if not address:
                self.send(chat_id, "Не понял адрес. Напишите ещё раз.", force_reply("Откуда"))
                return
            data["from"] = address
            self.store.set_session(self.name, chat_id, "to", data)
            self.send(
                chat_id,
                "🏁 <b>Куда едем?</b>\n\nНапишите адрес доставки.",
                force_reply("Куда"),
            )
            return

        if state == "to":
            address = clean_address(text)
            if not address:
                self.send(chat_id, "Не понял адрес. Напишите ещё раз.", force_reply("Куда"))
                return
            data["to"] = address
            self.store.set_session(self.name, chat_id, "comment", data)
            self.send(
                chat_id,
                "📝 <b>Комментарий</b>\n\nНапример: «подъезд 2, код 1234» "
                "или «есть багаж». Можно отправить <code>-</code>, чтобы пропустить.",
                force_reply("Комментарий или -"),
            )
            return

        if state == "comment":
            value = " ".join((text or "").split())
            if value in {"-", "нет", "нету"}:
                value = ""
            elif len(value) > 300:
                self.send(chat_id, "Слишком длинно, уложитесь в 300 символов.")
                return
            data["comment"] = value
            self.store.set_session(self.name, chat_id, "phone", data)
            self.send(
                chat_id,
                "📞 <b>Ваш телефон</b>\n\nНапишите номер, чтобы водитель мог "
                "позвонить. Или отправьте <code>-</code>, чтобы пропустить.",
                force_reply("+7..."),
            )
            return

        if state == "phone":
            phone = normalize_phone(text)
            if phone is None:
                self.send(chat_id, "Не похоже на номер. Пример: +79991234567", force_reply("+7..."))
                return
            data["phone"] = phone
            self.store.set_session(self.name, chat_id, "preview", data)
            self.send_preview(chat_id, data)
            return

    def handle_callback(self, callback):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        chat_id = chat["id"]
        user = callback.get("from") or {}
        if user.get("id") != chat_id:
            self.answer(callback, "Это не ваш заказ.", alert=True)
            return
        data = callback.get("data") or ""
        message_id = message.get("message_id")
        parts = data.split(":")

        if data.startswith("a:"):
            self.handle_admin_callback(callback, data, message_id)
            return

        if data == "u:new":
            self.answer(callback)
            self.start_order(chat_id)
            return

        if data == "u:edit":
            self.answer(callback, "Начнём заново")
            self.store.set_session(self.name, chat_id, "from", {})
            if message_id:
                self.edit(chat_id, message_id, "✏️ <b>Заполняем заново</b>", EMPTY_KEYBOARD)
            self.send(chat_id, "📍 <b>Откуда едем?</b>", force_reply("Откуда"))
            return

        if data == "u:send":
            key = f"u:send:{chat_id}"
            if not self.service.guard.claim(key):
                self.answer(callback, "Уже отправляю…")
                return
            self.finish_preview(chat_id, callback, message_id)
            return

        if data == "u:cancel_draft":
            self.answer(callback, "Отменил")
            self.store.clear_session(self.name, chat_id)
            if message_id:
                self.edit(
                    chat_id,
                    message_id,
                    "❌ <b>Оформление отменено</b>",
                    inline_keyboard([[button("🚕 Заказать такси", "u:new")]]),
                )
            return

        if parts[:1] == ["u"] and len(parts) == 3 and parts[1] == "cancel":
            self.answer(callback)
            try:
                order_id = int(parts[2])
            except ValueError:
                return
            order = self.store.get_order(order_id)
            if not order or order["client_id"] != chat_id:
                self.answer(callback, "Заказ не найден", alert=True)
                return
            if order["status"] not in ("new", "searching"):
                self.answer(callback, "Заказ уже принял водитель", alert=True)
                return
            if not self.service.change_status(
                order_id, "cancelled", actor_name="Клиент", actor_id=chat_id
            ):
                self.answer(callback, "Заказ уже нельзя отменить", alert=True)
                return
            self.answer(callback, "Заказ отменён")
            if message_id:
                fresh = self.store.get_order(order_id)
                self.edit(chat_id, message_id, client_cancelled_text(fresh), EMPTY_KEYBOARD)
            return

        if data == "u:status":
            self.answer(callback)
            self.show_status(chat_id, message_id)
            return

        self.answer(callback)

    # ------------------------------------------------------------------ flow
    def send_main(self, chat_id, edit_message_id=None):
        active = self.store.active_order_for_client(chat_id)
        if active:
            text = client_status_text(active)
            rows = [[button("📋 Подробнее", "u:status")]]
        else:
            text = (
                "🚕 <b>СИГМА</b>\n\n"
                "Заказ такси в пару сообщений. Нажмите «Заказать» — спрошу адрес "
                "подачи и адрес доставки, а затем передам заказ водителям.\n\n"
                "Когда заказ примут, пришлю ФИО и телефон водителя."
            )
            rows = [[button("🚕 Заказать такси", "u:new")]]
        if self.store.is_admin(chat_id):
            rows.append([button("⚙️ Админка", "a:panel")])
        keyboard = inline_keyboard(rows)
        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, keyboard)
        else:
            self.send(chat_id, text, keyboard)

    def start_order(self, chat_id):
        active = self.store.active_order_for_client(chat_id)
        if active:
            self.send(chat_id, f"У вас уже есть заказ <b>{public_id(active)}</b>.")
            return
        self.store.set_session(self.name, chat_id, "from", {})
        self.send(chat_id, "📍 <b>Откуда едем?</b>\n\nНапишите адрес подачи.", force_reply("Откуда"))

    def send_preview(self, chat_id, data):
        lines = [
            "🚕 <b>Проверьте заказ</b>",
            "",
            f"📍 <b>Откуда:</b> {esc(data.get('from'))}",
            f"🏁 <b>Куда:</b> {esc(data.get('to'))}",
        ]
        if data.get("comment"):
            lines.append(f"📝 <b>Комментарий:</b> {esc(data['comment'])}")
        if data.get("phone"):
            lines.append(f"📞 <b>Телефон:</b> {esc(data['phone'])}")
        lines += ["", "Отправляем водителям?"]
        self.send(
            chat_id,
            "\n".join(lines),
            inline_keyboard(
                [
                    [button("✅ Отправить", "u:send")],
                    [button("✏️ Изменить", "u:edit"), button("❌ Отмена", "u:cancel_draft")],
                ]
            ),
        )

    def finish_preview(self, chat_id, callback, message_id):
        session = self.store.get_session(self.name, chat_id)
        data = (session or {}).get("data") or {}
        if not session or session.get("state") != "preview" or not data.get("from"):
            self.answer(callback, "Заказ уже отправлен", alert=True)
            return
        if self.store.active_order_for_client(chat_id):
            self.answer(callback, "У вас уже есть активный заказ", alert=True)
            return

        client = self.store.get_client(chat_id) or {"chat_id": chat_id}
        order = self.store.create_order(
            client_id=chat_id,
            client_name=client.get("name"),
            client_username=client.get("username"),
            client_phone=data.get("phone") or client.get("phone"),
            from_addr=data["from"],
            to_addr=data["to"],
            comment=data.get("comment") or None,
            client_message_id=message_id,
        )
        if client.get("phone") != (data.get("phone") or None):
            self.store.ensure_client(chat_id, phone=data.get("phone") or None)

        self.store.clear_session(self.name, chat_id)
        self.answer(callback, "Отправляю водителям…")

        text = client_search_text(order)
        keyboard = client_keyboard(order)
        if message_id:
            if not self.edit(chat_id, message_id, text, keyboard):
                sent = self.send(chat_id, text, keyboard)
                if sent:
                    self.store.set_client_message(order["id"], sent["message_id"])
        else:
            sent = self.send(chat_id, text, keyboard)
            if sent:
                self.store.set_client_message(order["id"], sent["message_id"])

        threading.Thread(
            target=self._publish_safely, args=(order["id"],), daemon=True
        ).start()

    def _publish_safely(self, order_id):
        try:
            message_id = self.service.publish(order_id)
            if not message_id:
                self.send(
                    self.store.get_order(order_id)["client_id"],
                    "⚠️ Не удалось передать заказ водителям. Мы уже разбираемся, "
                    "попробуйте позже или позвоните диспетчеру.",
                )
        except Exception:
            log.exception("publish order %s failed", order_id)

    def show_status(self, chat_id, edit_message_id=None):
        order = self.store.active_order_for_client(chat_id)
        if not order:
            order = self.store.last_order_for_client(chat_id)
        if not order:
            if edit_message_id:
                self.edit(
                    chat_id,
                    edit_message_id,
                    "Заказов пока нет.",
                    inline_keyboard([[button("🚕 Заказать такси", "u:new")]]),
                )
            else:
                self.send(chat_id, "Заказов пока нет.")
            return
        text = client_status_text(order)
        keyboard = client_keyboard(order)
        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, keyboard)
        else:
            self.send(chat_id, text, keyboard)

    # -------------------------------------------------------------- commands
    def cmd_start(self, msg, args):
        chat_id = msg["chat"]["id"]
        self.store.clear_session(self.name, chat_id)
        if (args or "").strip().lower() in {"order", "taxi", "заказ"}:
            self.start_order(chat_id)
            return
        self.send_main(chat_id)

    cmd_help = cmd_start

    def cmd_status(self, msg, args):
        self.show_status(msg["chat"]["id"])

    def cmd_cancel(self, msg, args):
        chat_id = msg["chat"]["id"]
        if self.store.get_session(self.name, chat_id):
            self.store.clear_session(self.name, chat_id)
            self.send(chat_id, "Хорошо, отменил оформление.")
            self.send_main(chat_id)
            return
        order = self.store.active_order_for_client(chat_id)
        if not order:
            self.send(chat_id, "Активных заказов нет.")
            return
        if order["status"] not in ("new", "searching"):
            self.send(chat_id, "Заказ уже принял водитель, отменить нельзя.")
            return
        if self.service.change_status(order["id"], "cancelled", actor_name="Клиент", actor_id=chat_id):
            self.send(chat_id, "Заказ отменён.")
        else:
            self.send(chat_id, "Не получилось отменить — возможно, его уже взяли.")

    def cmd_id(self, msg, args):
        self.send(msg["chat"]["id"], f"Ваш Telegram ID: <code>{msg['chat']['id']}</code>")

    # ------------------------------------------------------------ /post (реклама)
    def cmd_post(self, msg, args):
        """Опубликовать в этом чате пост с кнопкой «Заказать такси»."""
        chat_id = msg["chat"]["id"]
        if is_private(msg):
            self.send(chat_id, "Команду <code>/post</code> пишите в группе или канале.")
            return
        if not self._can_manage(chat_id, msg.get("from") or {}):
            self.send(chat_id, "⛔ Публиковать посты может только администратор чата.")
            return
        text = self.post_text()
        keyboard = self.post_keyboard()
        photo = self.post_photo_path()
        try:
            if photo and os.path.exists(photo):
                result = self.api.send_photo(
                    chat_id, photo, caption=text, parse_mode="HTML",
                    reply_markup=keyboard,
                )
            else:
                result = self.api.send_message(
                    chat_id, text, reply_markup=keyboard,
                    link_preview_options={"is_disabled": False},
                )
        except TelegramError as exc:
            self.send(
                chat_id,
                f"⚠️ Не получилось опубликовать: {exc}\n\n"
                "Проверьте, что я админ чата и мне разрешено писать сообщения.",
            )
            return
        log.info("post опубликован в %s (%s)", chat_id, result.get("message_id"))
        if "pin" in (args or "").split():
            try:
                self.api.pin_chat_message(chat_id, result["message_id"])
                self.send(chat_id, "📌 Пост опубликован и закреплён.")
            except TelegramError as exc:
                log.warning("закрепить пост не вышло: %s", exc)

    def cmd_text(self, msg, args):
        """Заменить текст поста: /text многострочный текст."""
        chat_id = msg["chat"]["id"]
        if not self._can_manage(chat_id, msg.get("from") or {}):
            self.send(chat_id, "⛔ Менять текст поста может только администратор.")
            return
        new_text = (args or "").strip()
        if len(new_text) < 10:
            current = self.post_text()
            self.send(
                chat_id,
                "📄 <b>Текущий текст поста</b>\n\n"
                f"<pre>{esc(current)}</pre>\n\n"
                "Чтобы заменить: <code>/text новый текст поста</code>\n"
                "Поддерживается HTML: &lt;b&gt;, &lt;i&gt;, &lt;a href&gt;.",
            )
            return
        self.store.meta_set("post_text", new_text)
        self.send(chat_id, "✅ Текст поста обновлён. Проверьте: <code>/post</code>")
        log.info("post_text обновлён в %s", chat_id)

    def post_text(self):
        custom = self.store.meta_get("post_text")
        if custom:
            return custom
        username = self.bot_username or "TaxiSigmabot"
        return (
            "🚕 <b>ТАКСИ ЕНАКИЕВО</b>\n\n"
            "Заказ в 2 клика — без звонков и ожидания.\n"
            "Подача от 5 минут, работаем круглосуточно.\n\n"
            "👇 Нажмите кнопку ниже, чтобы вызвать машину."
        )

    def post_keyboard(self):
        url = self.store.meta_get("post_button_url")
        if not url:
            username = self.bot_username or "TaxiSigmabot"
            url = f"https://t.me/{username}?start=order"
        label = self.store.meta_get("post_button_text") or "Сделать заказ ✅"
        return {"inline_keyboard": [[{"text": label, "url": url}]]}

    def post_photo_path(self):
        photo = os.environ.get("POST_PHOTO") or os.path.join(BASE_DIR, "assets", "post.png")
        return photo if os.path.exists(photo) else None

    def cmd_button(self, msg, args):
        """Кнопка поста: /button Текст|https://ссылка"""
        chat_id = msg["chat"]["id"]
        if not self._can_manage(chat_id, msg.get("from") or {}):
            self.send(chat_id, "⛔ Менять кнопку может только администратор.")
            return
        raw = (args or "").strip()
        if "|" not in raw:
            self.send(
                chat_id,
                "🔗 <b>Кнопка поста</b>\n\n"
                f"Сейчас: <code>{esc(self.store.meta_get('post_button_text') or 'Сделать заказ ✅')}</code> → "
                f"<code>{esc(self.store.meta_get('post_button_url') or 'https://t.me/<бот>?start=order')}</code>\n\n"
                "Заменить: <code>/button Текст|https://ссылка</code>",
            )
            return
        label, _, url = raw.partition("|")
        self.store.meta_set("post_button_text", label.strip()[:64])
        self.store.meta_set("post_button_url", url.strip())
        self.send(chat_id, "✅ Кнопка обновлена.")

class GroupBot(BotBase):
    """Бот-диспетчер: работает в чате водителей и в личке водителя."""

    name = "group"
    commands = [
        ("start", "Регистрация водителя"),
        ("me", "Мой профиль"),
        ("stats", "Моя статистика"),
        ("admin", "Админка"),
        ("id", "ID этого чата"),
        ("online", "На смене"),
        ("offline", "Не на смене"),
        ("forward", "Переслать заказ в чат"),
        ("source", "Добавить этот чат в источники"),
        ("unsource", "Убрать этот чат из источников"),
        ("sources", "Список чатов-источников"),
        ("post", "Опубликовать пост с кнопкой"),
    ]

    def __init__(self, api, store, service, stop_event, admin_code="", order_ttl_minutes=30,
                 forward_min_score=2, forward_min_length=15, forward_dedupe_ttl=900,
                 forward_as_copy=True):
        super().__init__(api, store, service, stop_event, admin_code=admin_code)
        self.order_ttl_minutes = max(1, int(order_ttl_minutes or 30))
        self._last_ttl_check = 0.0
        self.forward_min_score = max(0, int(forward_min_score))
        self.forward_min_length = max(0, int(forward_min_length))
        self.forward_dedupe_ttl = max(60, int(forward_dedupe_ttl))
        self.forward_as_copy = bool(forward_as_copy)
        self._last_prune = 0.0

    def tick(self):
        """Автоотмена непринятых заказов — не чаще раза в 15 секунд."""
        now = time.monotonic()
        if now - self._last_ttl_check < 15:
            return
        self._last_ttl_check = now
        try:
            published = self.service.retry_unpublished()
            if published:
                log.info("досланы заказы: %s", published)
            expired = self.service.expire_stale(self.order_ttl_minutes * 60)
            if expired:
                log.info("автоотмена заказов: %s", expired)
        except Exception:
            log.exception("auto-expire failed")

        # чистка старых записей автопересылки — раз в час
        if now - self._last_prune > 3600:
            self._last_prune = now
            try:
                self.store.prune_forwarded()
            except Exception:
                log.exception("prune forwarded failed")

    def handle_message(self, msg):
        if msg.get("chat", {}).get("type") not in {"private", "group", "supergroup"}:
            return
        if not is_private(msg):
            # группа: команды + автопересылка заказов из чатов-источников
            self.handle_group(msg)
            return

        chat_id = msg["chat"]["id"]
        user = msg.get("from") or {}
        text = msg.get("text") or ""

        if text.startswith("/"):
            cmd, _, args = text.partition(" ")
            cmd = cmd.lstrip("/").split("@")[0].lower()
            handler = getattr(self, f"cmd_{cmd}", None)
            if handler:
                handler(msg, args.strip())
                return

        self.private_step(chat_id, text, user)

    def private_step(self, chat_id, text, user):
        session = self.store.get_session(self.name, chat_id)
        state = session["state"] if session else None
        data = dict(session.get("data") or {}) if session else {}

        if state == "admin_code":
            self.admin_code_step(chat_id, text)
            return

        if state == "full_name":
            name = clean_text(text, 3, 100)
            if not name:
                self.send(chat_id, "Напишите ФИО полностью, хотя бы 3 символа.")
                return
            data["full_name"] = name
            self.store.set_session(self.name, chat_id, "car", data)
            self.send(
                chat_id,
                "🚗 <b>Марка и модель машины</b>\n\n"
                "Например: <code>Kia Rio</code> или <code>Лада Гранта</code>.",
                force_reply("Kia Rio"),
            )
            return

        if state == "car":
            car = clean_text(text, 2, 60)
            if not car:
                self.send(
                    chat_id,
                    "Напишите марку и модель машины, например <code>Kia Rio</code>.",
                    force_reply("Kia Rio"),
                )
                return
            data["car"] = car
            self.store.set_session(self.name, chat_id, "color", data)
            self.send(
                chat_id,
                "🎨 <b>Цвет машины</b>\n\nНапример: <code>белый</code>, "
                "<code>чёрный</code>, <code>серебристый</code>.",
                force_reply("белый"),
            )
            return

        if state == "color":
            color = clean_text(text, 2, 30)
            if not color:
                self.send(
                    chat_id,
                    "Напишите цвет машины, например <code>белый</code>.",
                    force_reply("белый"),
                )
                return
            data["color"] = color
            self.store.set_session(self.name, chat_id, "plate", data)
            self.send(
                chat_id,
                "🔢 <b>Гос. номер машины</b>\n\n"
                "Например: <code>А123ВС77</code> или <code>1234 AB 77</code>.",
                force_reply("А123ВС77"),
            )
            return

        if state == "plate":
            plate = clean_text(text, 2, 20)
            if not plate:
                self.send(
                    chat_id,
                    "Напишите гос. номер, например <code>А123ВС77</code>.",
                    force_reply("А123ВС77"),
                )
                return
            data["plate"] = plate.upper()
            self.store.set_session(self.name, chat_id, "phone", data)
            self.send(
                chat_id,
                "📞 <b>Телефон для связи</b>\n\n"
                "Напишите номер или <code>-</code>, чтобы пропустить.",
                force_reply("+7..."),
            )
            return

        if state == "phone":
            phone = normalize_phone(text)
            if phone is None:
                self.send(chat_id, "Не похоже на номер. Пример: +79991234567")
                return
            self.store.create_courier(
                chat_id,
                data["full_name"],
                phone=phone or None,
                car=data.get("car"),
                color=data.get("color"),
                plate=data.get("plate"),
                username=user.get("username"),
            )
            self.store.clear_session(self.name, chat_id)
            self.send(
                chat_id,
                f"✅ <b>Готово!</b>\n\n"
                f"Вы зарегистрированы как <b>{esc(data['full_name'])}</b>.\n"
                f"🚗 {esc(data.get('car'))}, {esc(data.get('color'))}, "
                f"<b>{esc(data.get('plate'))}</b>\n\n"
                f"Теперь можно принимать заказы в чате водителей.",
                remove_keyboard(),
            )
            self.send_courier_menu(chat_id)
            return

        if self.store.get_courier(chat_id):
            self.send_courier_menu(chat_id)
            return
        self.begin_registration(chat_id)

    def begin_registration(self, chat_id):
        self.store.set_session(self.name, chat_id, "full_name", {})
        self.send(
            chat_id,
            "🙋 <b>Регистрация водителя СИГМА</b>\n\n"
            "Спрошу 5 пунктов: ФИО, машину, цвет, гос. номер и телефон.\n"
            "Всё это будет видно клиенту и в чате заказов.\n\n"
            "Напишите ваше <b>ФИО</b>:",
            force_reply("Иванов Иван Иванович"),
        )

    def send_courier_menu(self, chat_id):
        self.show_courier_menu(chat_id)

    # -------------------------------------------------------------- callbacks
    def handle_callback(self, callback):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user = callback.get("from") or {}
        data = callback.get("data") or ""
        parts = data.split(":")
        message_id = message.get("message_id")

        if data.startswith("a:"):
            self.handle_admin_callback(callback, data, message_id)
            return

        # --- принятие/статусы заказа в группе
        if parts[:1] == ["c"] and len(parts) == 3:
            self.handle_group_callback(callback, parts[1], parts[2], chat, user, message_id)
            return

        # --- личное меню водителя
        if parts[:1] == ["d"] and len(parts) >= 2 and is_private(message):
            self.handle_courier_callback(callback, parts, chat_id, user, message_id)
            return

        self.answer(callback)

    def handle_group_callback(self, callback, action, raw_id, chat, user, message_id):
        try:
            order_id = int(raw_id)
        except ValueError:
            self.answer(callback)
            return

        # Защита от двойных нажатий: одинаковый клик обрабатываем один раз.
        guard_key = f"cb:{user.get('id')}:{action}:{order_id}"
        if not self.service.guard.claim(guard_key):
            self.answer(callback, "Уже обрабатывается…")
            return

        order = self.store.get_order(order_id)
        if not order:
            self.answer(callback, "Заказ не найден", alert=True)
            return

        if action == "accept":
            courier = self.store.get_courier(user["id"])
            if not courier:
                self.answer(
                    callback,
                    "Сначала зарегистрируйтесь в личке бота — напишите ему /start.",
                    alert=True,
                )
                return
            if self.store.active_order_for_courier(user["id"]):
                self.answer(callback, "У вас уже есть активный заказ", alert=True)
                return
            accepted = self.service.accept(order_id, courier)
            if not accepted:
                self.answer(callback, "Поздно 😅 заказ уже взяли", alert=True)
                return
            self.answer(callback, f"Заказ {public_id(accepted)} ваш!")
            return

        # остальные действия — только принявший водитель
        if order.get("courier_id") != user["id"]:
            self.answer(callback, "Это не ваш заказ", alert=True)
            return

        courier = self.store.get_courier(user["id"]) or {}
        actor = courier.get("full_name") or display_name(user)

        if action == "onway":
            if order["status"] != "accepted":
                self.answer(callback, "Статус уже изменился", alert=True)
                return
            self.service.change_status(
                order_id, "on_way", courier_id=user["id"], actor_name=actor, actor_id=user["id"]
            )
            self.answer(callback, "Отмечено: выехали")
            return

        if action == "done":
            if order["status"] not in ("accepted", "on_way"):
                self.answer(callback, "Заказ уже закрыт", alert=True)
                return
            self.service.change_status(
                order_id, "done", courier_id=user["id"], actor_name=actor, actor_id=user["id"]
            )
            self.answer(callback, "Заказ завершён 🏁")
            return

        if action == "cancel":
            if order["status"] not in ("accepted", "on_way"):
                self.answer(callback, "Заказ уже закрыт", alert=True)
                return
            self.service.change_status(
                order_id, "cancelled", courier_id=user["id"], actor_name=actor, actor_id=user["id"]
            )
            self.answer(callback, "Вы отказались от заказа")
            return

        self.answer(callback)

    def handle_courier_callback(self, callback, parts, chat_id, user, message_id):
        action = parts[1]
        if action == "toggle" and len(parts) == 3:
            status = "active" if parts[2] == "online" else "offline"
            self.store.set_courier_status(chat_id, status)
            self.answer(callback, "Вы на смене" if status == "active" else "Вы не на смене")
            if message_id:
                self.show_courier_menu(chat_id, message_id)
            return

        if action == "stats":
            self.answer(callback)
            self.show_courier_stats(chat_id, message_id)
            return

        if action == "menu":
            self.answer(callback)
            self.show_courier_menu(chat_id, message_id)
            return

        if action == "view" and len(parts) == 3:
            order = self.store.get_order(int(parts[2]))
            self.answer(callback)
            if order:
                self.send(chat_id, client_status_text(order))
            return

        self.answer(callback)

    def show_courier_menu(self, chat_id, edit_message_id=None):
        courier = self.store.get_courier(chat_id)
        if not courier:
            self.begin_registration(chat_id)
            return
        active = self.store.active_order_for_courier(chat_id)
        stats = self.store.courier_stats(chat_id)
        lines = []
        if active:
            lines += [
                f"🚕 Текущий заказ <b>{public_id(active)}</b>",
                f"🏁 {esc(active['to_addr'])}",
                "",
            ]
        lines += [
            f"📦 Всего взято: <b>{stats['total']}</b>",
            f"🏁 Выполнено: <b>{stats['done']}</b>",
            f"📅 Сегодня: <b>{stats['today_done']}</b>",
        ]
        rows = []
        if active:
            rows.append([button("📋 Текущий заказ", f"d:view:{active['id']}")])
        rows.append([button("📊 Подробная статистика", "d:stats")])
        if self.store.is_admin(chat_id):
            rows.append([button("⚙️ Админка", "a:panel")])
        toggle = "offline" if courier["status"] == "active" else "online"
        label = "🔴 Уйти со смены" if toggle == "offline" else "🟢 Выйти на смену"
        rows.append([button(label, f"d:toggle:{toggle}")])
        keyboard = inline_keyboard(rows)
        if edit_message_id:
            self.edit(chat_id, edit_message_id, "\n".join(lines), keyboard)
        else:
            self.send(chat_id, "\n".join(lines), keyboard)

    def show_courier_stats(self, chat_id, edit_message_id=None):
        courier = self.store.get_courier(chat_id)
        if not courier:
            self.begin_registration(chat_id)
            return
        stats = self.store.courier_stats(chat_id)
        text = courier_stats_text(courier, stats)
        keyboard = inline_keyboard([[button("⬅️ Назад", "d:menu")]])
        if edit_message_id:
            self.edit(chat_id, edit_message_id, text, keyboard)
        else:
            self.send(chat_id, text, keyboard)

    # -------------------------------------------------------------- commands
    def cmd_start(self, msg, args):
        chat_id = msg["chat"]["id"]
        user = msg.get("from") or {}
        if is_group(msg):
            self.send(
                chat_id,
                "🚕 <b>Бот заказов СИГМА</b> уже на месте.\n"
                "Новые заказы появятся здесь автоматически.",
            )
            return
        courier = self.store.get_courier(chat_id)
        if courier:
            self.send_courier_menu(chat_id)
            return
        self.begin_registration(chat_id)

    cmd_help = cmd_start

    def cmd_id(self, msg, args):
        chat_id = msg["chat"]["id"]
        self.send(
            chat_id,
            f"ID чата: <code>{chat_id}</code>\n"
            "Впишите его в <code>GROUP_CHAT_ID</code> в .env, если ещё не сделали.",
        )

    def cmd_me(self, msg, args):
        if not is_private(msg):
            return
        self.show_courier_menu(msg["chat"]["id"])

    def cmd_stats(self, msg, args):
        if not is_private(msg):
            self.send(msg["chat"]["id"], "Статистика доступна в личке бота.")
            return
        chat_id = msg["chat"]["id"]
        if not self.store.get_courier(chat_id):
            self.begin_registration(chat_id)
            return
        self.show_courier_stats(chat_id)

    def cmd_admin(self, msg, args):
        chat_id = msg["chat"]["id"]
        if not is_private(msg):
            self.send(chat_id, "Админка открывается в личке бота.")
            return
        if self.store.is_admin(chat_id):
            self.show_admin_panel(chat_id)
        else:
            self.begin_admin_login(chat_id)

    def cmd_online(self, msg, args):
        chat_id = msg["chat"]["id"]
        if not self.store.get_courier(chat_id):
            self.begin_registration(chat_id)
            return
        self.store.set_courier_status(chat_id, "active")
        self.send(chat_id, "🟢 Вы на смене.")

    def cmd_offline(self, msg, args):
        chat_id = msg["chat"]["id"]
        self.store.set_courier_status(chat_id, "offline")
        self.send(chat_id, "⚪ Вы не на смене.")

    def cmd_forward(self, msg, args):
        """Переслать заказ из другого чата в чат водителей.

        Использование: ответьте командой /forward на сообщение с заказом.
        """
        chat_id = msg["chat"]["id"]
        reply = msg.get("reply_to_message")
        if not reply:
            self.send(
                chat_id,
                "Ответьте командой <code>/forward</code> на сообщение с заказом, "
                "которое нужно переслать в чат водителей.",
            )
            return
        if not self.service.group_chat_id:
            self.send(chat_id, "⚠️ GROUP_CHAT_ID не настроен — некуда пересылать.")
            return
        try:
            self.service.group_api.forward_message(
                self.service.group_chat_id,
                chat_id,
                reply["message_id"],
            )
        except TelegramError as exc:
            self.send(chat_id, f"⚠️ Не удалось переслать: {exc}")
            return
        self.send(chat_id, "✅ Заказ переслан в чат водителей.")


# ======================================================================= main
def main():
    load_env_file(os.path.join(BASE_DIR, ".env"))

    parser = argparse.ArgumentParser(description="СИГМА: два Telegram-бота для заказов")
    parser.add_argument("--client-token", default=os.environ.get("CLIENT_BOT_TOKEN"))
    parser.add_argument("--group-token", default=os.environ.get("GROUP_BOT_TOKEN"))
    parser.add_argument("--group-chat-id", default=os.environ.get("GROUP_CHAT_ID"))
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data")))
    parser.add_argument("--api-base", default=os.environ.get("API_BASE", "https://api.telegram.org"))
    parser.add_argument(
        "--order-ttl",
        type=int,
        default=int(os.environ.get("ORDER_TTL_MINUTES", "30")),
        help="Через сколько минут автоматически отменять непринятый заказ",
    )
    parser.add_argument("--admin-code", default=os.environ.get("ADMIN_CODE", ""))
    parser.add_argument(
        "--forward-to",
        default=os.environ.get("FORWARD_TO", ""),
        help="Куда слать заказы: ID чата/канала через запятую",
    )
    parser.add_argument(
        "--forward-header",
        default=os.environ.get("FORWARD_HEADER", ""),
        help="Строка перед заказом, можно {source}; пусто — без заголовка",
    )
    parser.add_argument(
        "--forward-extra-triggers",
        default=os.environ.get("EXTRA_TRIGGERS", ""),
        help="Свои фразы-триггеры через запятую",
    )
    parser.add_argument(
        "--forward-extra-negatives",
        default=os.environ.get("EXTRA_NEGATIVES", ""),
        help="Свои стоп-фразы через запятую",
    )
    parser.add_argument(
        "--forward-min-score",
        type=int,
        default=int(os.environ.get("FORWARD_MIN_SCORE", "2")),
        help="Порог триггеров для автопересылки заказов",
    )
    parser.add_argument(
        "--forward-min-length",
        type=int,
        default=int(os.environ.get("FORWARD_MIN_LENGTH", "15")),
        help="Минимальная длина сообщения для автопересылки",
    )
    parser.add_argument(
        "--forward-dedupe-ttl",
        type=int,
        default=int(os.environ.get("FORWARD_DEDUPE_TTL", "900")),
        help="Сколько секунд не пересылать одинаковые заказы",
    )
    parser.add_argument(
        "--forward-no-copy",
        action="store_true",
        default=os.environ.get("FORWARD_AS_COPY", "1") in {"0", "false", "False"},
        help="Пересылать с плашкой «переслано», а не копией",
    )
    args = parser.parse_args()

    if not os.path.isabs(args.data_dir):
        args.data_dir = os.path.join(BASE_DIR, args.data_dir)

    if not args.client_token:
        parser.error("CLIENT_BOT_TOKEN и GROUP_BOT_TOKEN обязательны (в .env или аргументами)")

    group_chat_id = None
    if args.group_chat_id:
        try:
            group_chat_id = int(args.group_chat_id)
        except ValueError:
            parser.error("GROUP_CHAT_ID должен быть числом (узнать: /id в группе)")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    os.makedirs(args.data_dir, exist_ok=True)
    store = Store(os.path.join(args.data_dir, "sigma.db"))
    client_api = BotAPI(args.client_token, args.api_base)
    group_api = BotAPI(args.group_token or args.client_token, args.api_base)
    service = SigmaService(store, client_api, group_api, group_chat_id)

    stop = threading.Event()
    client_bot = ClientBot(client_api, store, service, stop, admin_code=args.admin_code)
    client_bot.forward_min_score = args.forward_min_score
    client_bot.forward_min_length = args.forward_min_length
    client_bot.forward_dedupe_ttl = args.forward_dedupe_ttl
    client_bot.forward_as_copy = not args.forward_no_copy
    group_bot = GroupBot(
        group_api, store, service, stop,
        admin_code=args.admin_code,
        order_ttl_minutes=args.order_ttl,
        forward_min_score=args.forward_min_score,
        forward_min_length=args.forward_min_length,
        forward_dedupe_ttl=args.forward_dedupe_ttl,
        forward_as_copy=not args.forward_no_copy,
    )

    # общие настройки автопересылки для обоих ботов
    forward_to = _parse_ids(args.forward_to)
    extra_triggers = _split_csv(args.forward_extra_triggers)
    extra_negatives = _split_csv(args.forward_extra_negatives)
    for bot_obj in (client_bot, group_bot):
        bot_obj.forward_to = forward_to
        bot_obj.forward_header = args.forward_header
        bot_obj.forward_with_header = bool(args.forward_header.strip())
        bot_obj.forward_extra_triggers = extra_triggers
        bot_obj.forward_extra_negatives = extra_negatives

    threads = [
        threading.Thread(target=_run_bot, args=(client_bot, stop), name="client", daemon=True),
    ]
    if args.group_token:
        threads.append(
            threading.Thread(target=_run_bot, args=(group_bot, stop), name="group", daemon=True)
        )

    def shutdown(*_):
        log.info("останавливаюсь…")
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info(
        "старт: group_chat_id=%s, водителей=%s, автоотмена=%s мин, админка=%s, "
        "источников=%s, приёмников=%s, автопересылка=%s (порог %s, копия=%s)",
        group_chat_id or "не задан",
        store.courier_count(),
        args.order_ttl,
        "on" if args.admin_code else "off",
        len(store.forward_sources(only_enabled=True)),
        forward_to or [r["chat_id"] for r in store.forward_targets(only_enabled=True)],
        "on",
        args.forward_min_score,
        "yes" if not args.forward_no_copy else "no",
    )
    for thread in threads:
        thread.start()
    while any(t.is_alive() for t in threads):
        time.sleep(0.5)
    for thread in threads:
        thread.join(timeout=5)


def _split_csv(raw):
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _parse_ids(raw):
    out = []
    for part in _split_csv(raw):
        try:
            out.append(int(part))
        except ValueError:
            log.warning("FORWARD_TO: %r не число — пропущен", part)
    return out


def _run_bot(bot, stop):
    try:
        bot.run()
    except Exception:
        log.exception("%s упал", bot.name)
        stop.set()


if __name__ == "__main__":
    main()

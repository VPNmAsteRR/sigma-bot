"""Логика заказов и форматирование карточек «Сигма».

Без цен и тарифов. Карточка в чате водителей короткая:
клиент → куда → кнопка «Принять заказ».
"""

import html
import logging
import threading
import time

from tgapi import TelegramError

log = logging.getLogger("sigma.core")

EMPTY_KEYBOARD = {"inline_keyboard": []}

# Статусы, при которых заказ ещё живой.
ACTIVE_STATUSES = ("new", "searching", "accepted", "on_way")


def esc(value):
    return html.escape(str(value if value is not None else ""))


def public_id(order):
    return f"#{int(order['id']):04d}"


def username_line(username):
    return f" (@{esc(username)})" if username else ""


def client_label(order):
    name = esc(order.get("client_name") or "Клиент")
    return f"{name}{username_line(order.get('client_username'))}"


def courier_label(order):
    return esc(order.get("courier_name") or "—")


def courier_car_line(entity):
    """Строка «марка, цвет, гос. номер» для карточек и профиля.

    Понимает и поля заказа (courier_car/...), и поля водителя (car/...).
    """
    def pick(name):
        return (entity.get(f"courier_{name}") or entity.get(name) or "").strip()

    parts = [esc(pick("car")), esc(pick("color")), esc(pick("plate"))]
    return ", ".join(p for p in parts if p)


def format_time(ts):
    if not ts:
        return "—"
    return time.strftime("%H:%M", time.localtime(int(ts)))


def format_duration(seconds):
    seconds = max(0, int(seconds or 0))
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    minutes = minutes % 60
    if hours < 24:
        return f"{hours} ч {minutes} мин"
    days = hours // 24
    return f"{days} д {hours % 24} ч"


# ------------------------------------------------------------------ guard
class ActionGuard:
    """Защита от повторных нажатий.

    Один и тот же callback (заказ + действие) нельзя обрабатывать дважды:
    второй клик получает «уже обрабатывается». Плюс блокировки на заказ,
    чтобы параллельные действия по одному заказу шли строго по очереди.
    """

    def __init__(self, ttl=4.0):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._recent = {}
        self._order_locks = {}

    def order_lock(self, order_id):
        with self._lock:
            lock = self._order_locks.get(order_id)
            if lock is None:
                lock = threading.RLock()
                self._order_locks[order_id] = lock
            return lock

    def claim(self, key):
        """True, если действие можно выполнять. False — это дубль."""
        now = time.monotonic()
        with self._lock:
            for old_key, ts in list(self._recent.items()):
                if now - ts > self.ttl:
                    self._recent.pop(old_key, None)
            if key in self._recent and now - self._recent[key] <= self.ttl:
                return False
            self._recent[key] = now
            return True

    def release(self, key):
        with self._lock:
            self._recent.pop(key, None)


# --------------------------------------------------------------- group card
def group_card_text(order):
    """Короткая карточка заказа в чате водителей: клиент → куда."""
    status = order.get("status")
    lines = [
        "🚕 <b>НОВЫЙ ЗАКАЗ</b> "
        f"<code>{public_id(order)}</code>",
        "",
        f"👤 <b>Клиент:</b> {client_label(order)}",
    ]
    if order.get("client_phone"):
        lines.append(f"📞 <b>Телефон:</b> {esc(order['client_phone'])}")
    lines += [
        "",
        f"🏁 <b>Куда:</b> {esc(order['to_addr'])}",
    ]
    if order.get("from_addr"):
        lines.append(f"📍 <b>Откуда:</b> {esc(order['from_addr'])}")
    if order.get("comment"):
        lines.append(f"📝 <b>Комментарий:</b> {esc(order['comment'])}")

    if order.get("courier_id"):
        lines += [
            "",
            "━━━━━━━━━━━━━━━",
            f"✅ <b>Принял:</b> {courier_label(order)}",
        ]
        car = courier_car_line(order)
        if car:
            lines.append(f"🚗 <b>Машина:</b> {car}")
        lines += [
            f"📞 <b>Телефон:</b> {esc(order.get('courier_phone') or '—')}",
            f"🕒 <b>Время:</b> {format_time(order.get('accepted_at'))}",
        ]

    status_text = {
        "new": "🟡 Готовится к отправке",
        "searching": "🟡 Ждёт водителя",
        "accepted": "🟢 Принят",
        "on_way": "🚗 В пути",
        "done": "🏁 Выполнен",
        "cancelled": "❌ Отменён",
    }.get(status, status)
    lines += ["", f"<b>{status_text}</b>"]

    if status == "searching":
        lines.append("<i>Нажмите «Принять заказ»</i>")
    return "\n".join(lines)


def group_keyboard(order):
    oid = order["id"]
    if order["status"] in ("new", "searching"):
        return {
            "inline_keyboard": [
                [{"text": "✅ Принять заказ", "callback_data": f"c:accept:{oid}"}]
            ]
        }
    if order["status"] == "accepted":
        return {
            "inline_keyboard": [
                [{"text": "🚗 Выехал к клиенту", "callback_data": f"c:onway:{oid}"}],
                [{"text": "❌ Отказаться", "callback_data": f"c:cancel:{oid}"}],
            ]
        }
    if order["status"] == "on_way":
        return {
            "inline_keyboard": [
                [{"text": "🏁 Доставил заказ", "callback_data": f"c:done:{oid}"}],
                [{"text": "❌ Отменить", "callback_data": f"c:cancel:{oid}"}],
            ]
        }
    return EMPTY_KEYBOARD


# ------------------------------------------------------------- client cards
def client_status_text(order):
    status = order["status"]
    lines = [f"🚕 <b>Заказ {public_id(order)}</b>", ""]
    lines += [
        f"📍 <b>Откуда:</b> {esc(order['from_addr'])}",
        f"🏁 <b>Куда:</b> {esc(order['to_addr'])}",
    ]
    if order.get("comment"):
        lines.append(f"📝 <b>Комментарий:</b> {esc(order['comment'])}")
    lines.append("")

    if status in ("new", "searching"):
        lines += [
            "🔎 <b>Ищем водителя…</b>",
            "Как только кто-то примет заказ, пришлю его ФИО и телефон.",
        ]
    elif status in ("accepted", "on_way"):
        lines += [
            "✅ <b>Водитель назначен!</b>",
            "",
            f"👤 <b>ФИО:</b> {courier_label(order)}",
            f"📞 <b>Телефон:</b> {esc(order.get('courier_phone') or '—')}",
        ]
        car = courier_car_line(order)
        if car:
            lines.append(f"🚗 <b>Машина:</b> {car}")
        if status == "on_way":
            lines.append("🚗 <b>Водитель уже выехал к вам.</b>")
    elif status == "done":
        lines += [
            "🏁 <b>Заказ выполнен.</b>",
            "Спасибо, что выбрали СИГМУ!",
        ]
    elif status == "cancelled":
        lines.append("❌ <b>Заказ отменён.</b>")

    return "\n".join(lines)


def client_keyboard(order):
    rows = []
    if order["status"] in ("new", "searching"):
        rows.append(
            [{"text": "❌ Отменить заказ", "callback_data": f"u:cancel:{order['id']}"}]
        )
    return {"inline_keyboard": rows}


def client_search_text(order):
    return (
        "⏳ <b>Заказ отправлен водителям</b>\n\n"
        f"{client_status_text(order)}\n\n"
        "<i>Можно отменить, пока заказ не приняли.</i>"
    )


def client_cancelled_text(order):
    return (
        "❌ <b>Заказ отменён</b>\n\n"
        f"Заказ {public_id(order)} больше не активен.\n"
        "Если нужна машина — создайте новый заказ."
    )


# ------------------------------------------------------------- courier stats
def courier_profile_text(courier):
    """Профиль водителя с машиной."""
    lines = [
        f"👤 <b>{esc(courier['full_name'])}</b>",
        f"📞 {esc(courier.get('phone') or '—')}",
    ]
    car = courier_car_line(courier)
    if car:
        lines.append(f"🚗 {car}")
    lines.append(
        f"Статус: {'🟢 на смене' if courier.get('status') == 'active' else '⚪ не на смене'}"
    )
    return "\n".join(lines)


def courier_stats_text(courier, stats):
    lines = [
        f"📊 <b>Статистика водителя</b>",
        "",
        f"👤 <b>{esc(courier['full_name'])}</b>",
        f"📞 {esc(courier.get('phone') or '—')}",
    ]
    car = courier_car_line(courier)
    if car:
        lines.append(f"🚗 {car}")
    lines += [
        f"Статус: {'🟢 на смене' if courier.get('status') == 'active' else '⚪ не на смене'}",
        "",
        "━━━━━━━━━━━━━━━",
        f"📦 <b>Всего заказов:</b> {stats['total']}",
        f"🏁 <b>Выполнено:</b> {stats['done']}",
        f"🚗 <b>В работе:</b> {stats['active']}",
        f"❌ <b>Отменено:</b> {stats['cancelled']}",
        "",
        f"📅 <b>Сегодня:</b> {stats['today_done']} выполнено "
        f"из {stats['today_total']} взятых",
        f"🗓 <b>За 7 дней:</b> {stats['week_done']} выполнено",
    ]
    if stats.get("avg_accept_seconds") is not None and stats["total"]:
        lines.append(
            f"⚡ <b>Среднее принятие:</b> {format_duration(stats['avg_accept_seconds'])}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------- admin stats
def admin_panel_text(stats):
    lines = [
        "⚙️ <b>Админка СИГМА</b>",
        "",
        "━━━━━━━━━━━━━━━",
        f"🚕 <b>Всего заказов:</b> {stats['orders_total']}",
        f"🔎 <b>Ждут водителя:</b> {stats['searching']}",
        f"🚗 <b>В работе:</b> {stats['in_progress']}",
        f"🏁 <b>Выполнено:</b> {stats['done']}",
        f"❌ <b>Отменено:</b> {stats['cancelled']}",
        "",
        f"📅 <b>Сегодня:</b> создано {stats['today_total']}, "
        f"выполнено {stats['today_done']}, отменено {stats['today_cancelled']}",
        "",
        "━━━━━━━━━━━━━━━",
        f"👥 <b>Водителей:</b> {stats['couriers_total']} "
        f"(на смене {stats['couriers_active']})",
    ]
    if stats.get("avg_accept_seconds") is not None and stats["done"]:
        lines.append(
            f"⚡ <b>Среднее принятие:</b> {format_duration(stats['avg_accept_seconds'])}"
        )
    return "\n".join(lines)


def admin_panel_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "👥 Водители", "callback_data": "a:drivers"}],
            [{"text": "📦 Последние заказы", "callback_data": "a:orders"}],
        ]
    }


def admin_drivers_text(couriers):
    if not couriers:
        return "👥 <b>Водители</b>\n\nПока никто не зарегистрирован."
    lines = ["👥 <b>Водители</b>", "", "━━━━━━━━━━━━━━━"]
    for c in couriers:
        shift = "🟢" if c.get("status") == "active" else "⚪"
        lines.append(
            f"{shift} <b>{esc(c['full_name'])}</b> — "
            f"{c['done_count']} выполнено, {c['cancel_count']} отменено"
        )
        if c.get("phone"):
            lines.append(f"   📞 {esc(c['phone'])}")
        car = courier_car_line(c)
        if car:
            lines.append(f"   🚗 {car}")
    return "\n".join(lines)


def admin_orders_text(orders):
    if not orders:
        return "📦 <b>Последние заказы</b>\n\nЗаказов пока нет."
    icon = {
        "new": "🟡",
        "searching": "🟡",
        "accepted": "🟢",
        "on_way": "🚗",
        "done": "🏁",
        "cancelled": "❌",
    }
    lines = ["📦 <b>Последние заказы</b>", ""]
    for o in orders:
        lines.append(
            f"{icon.get(o['status'], '•')} <b>{public_id(o)}</b> "
            f"{esc(o['to_addr'])}"
        )
        who = o.get("courier_name") or "не принят"
        lines.append(
            f"   👤 {esc(o.get('client_name') or 'Клиент')} → {esc(who)}"
        )
    return "\n".join(lines)


class SigmaService:
    """Публикация в чат, принятие, смена статуса, уведомления."""

    def __init__(self, store, client_api, group_api, group_chat_id):
        self.store = store
        self.client_api = client_api  # бот заказчика
        self.group_api = group_api  # бот диспетчера (в чате водителей)
        self.group_chat_id = group_chat_id
        self.guard = ActionGuard()

    # ------------------------------------------------------------- publishing
    def publish(self, order_id):
        """Опубликовать заказ в чат водителей. Возвращает message_id."""
        order = self.store.get_order(order_id)
        if not order:
            return None
        if not self.group_chat_id:
            log.warning("GROUP_CHAT_ID не задан — заказ %s не опубликован", order_id)
            return None
        # Рендерим карточку сразу в статусе «ждёт водителя», иначе в чат уйдёт
        # «готовится к отправке», хотя заказ уже там.
        preview = dict(order, status="searching")
        try:
            message = self.group_api.send_message(
                self.group_chat_id,
                group_card_text(preview),
                reply_markup=group_keyboard(preview),
            )
        except TelegramError as exc:
            log.error("не удалось опубликовать заказ %s: %s", order_id, exc)
            return None
        self.store.set_group_message(
            order_id, self.group_chat_id, message["message_id"]
        )
        self.refresh_client(order_id)
        return message["message_id"]

    def refresh_group(self, order_id):
        order = self.store.get_order(order_id)
        if not order or not order.get("group_chat_id") or not order.get("group_message_id"):
            return
        try:
            self.group_api.edit_message_text(
                order["group_chat_id"],
                order["group_message_id"],
                group_card_text(order),
                reply_markup=group_keyboard(order),
            )
        except TelegramError as exc:
            if "message is not modified" not in str(exc).lower():
                log.warning("group edit %s failed: %s", order_id, exc)

    def refresh_client(self, order_id):
        order = self.store.get_order(order_id)
        if not order:
            return
        if not order.get("client_message_id"):
            try:
                message = self.client_api.send_message(
                    order["client_id"],
                    client_status_text(order),
                    reply_markup=client_keyboard(order),
                )
            except TelegramError as exc:
                log.warning("client send %s failed: %s", order_id, exc)
                return
            self.store.set_client_message(order_id, message["message_id"])
            return
        try:
            self.client_api.edit_message_text(
                order["client_id"],
                order["client_message_id"],
                client_status_text(order),
                reply_markup=client_keyboard(order),
            )
        except TelegramError as exc:
            if "message is not modified" not in str(exc).lower():
                log.warning("client edit %s failed: %s", order_id, exc)

    def notify_client(self, order_id, text, keyboard=None):
        order = self.store.get_order(order_id)
        if not order:
            return
        try:
            self.client_api.send_message(
                order["client_id"], text, reply_markup=keyboard
            )
        except TelegramError as exc:
            log.warning("client notify %s failed: %s", order_id, exc)

    # ----------------------------------------------------------------- accept
    def accept(self, order_id, courier):
        """Принять заказ. Возвращает заказ или None (уже занят/дубль)."""
        with self.guard.order_lock(order_id):
            order = self.store.accept_order(order_id, courier)
            if not order:
                return None
            self.refresh_group(order_id)
            self.refresh_client(order_id)
            # Клиенту — пуш, иначе он не заметит правку карточки.
            self.notify_client(
                order_id,
                "✅ <b>Ваш заказ принят!</b>\n\n" + client_status_text(order),
            )
            return order

    # ----------------------------------------------------------------- status
    def change_status(self, order_id, status, courier_id=None, actor_name=None, actor_id=None):
        with self.guard.order_lock(order_id):
            ok = self.store.set_order_status(
                order_id, status, courier_id=courier_id,
                actor_name=actor_name, actor_id=actor_id,
            )
            if not ok:
                return False
            order = self.store.get_order(order_id)
            self.refresh_group(order_id)
            self.refresh_client(order_id)
            self._push_status(order, status)
            return True

    def _push_status(self, order, status):
        """Короткое уведомление клиенту о смене статуса."""
        if not order:
            return
        if status == "on_way":
            text = (
                "🚗 <b>Водитель выехал к вам</b>\n\n"
                f"👤 {courier_label(order)}\n"
                f"📞 {esc(order.get('courier_phone') or '—')}"
            )
            car = courier_car_line(order)
            if car:
                text += f"\n🚗 {car}"
        elif status == "done":
            text = (
                f"🏁 <b>Заказ {public_id(order)} выполнен</b>\n\n"
                "Спасибо, что выбрали СИГМУ!"
            )
        elif status == "cancelled":
            text = client_cancelled_text(order)
        else:
            return
        self.notify_client(order["id"], text)

    # ---------------------------------------------------------------- timeout
    def retry_unpublished(self, min_age_seconds=5):
        """Дослать заказы, которые не удалось опубликовать (бот вне чата и т.п.)."""
        published = []
        for order in self.store.unpublished_orders(min_age_seconds):
            if self.publish(order["id"]):
                published.append(order["id"])
        return published

    def expire_stale(self, max_age_seconds):
        """Автоотмена заказов, которые никто не взял за отведённое время."""
        stale = self.store.stale_searching_orders(max_age_seconds)
        expired = []
        for order in stale:
            if self.change_status(
                order["id"], "cancelled", actor_name="Автоотмена", actor_id=None
            ):
                try:
                    self.client_api.send_message(
                        order["client_id"],
                        f"⌛ <b>Заказ {public_id(order)} не приняли за "
                        f"{format_duration(max_age_seconds)}</b>\n\n"
                        "Мы отменили его автоматически. Попробуйте оформить снова.",
                    )
                except TelegramError as exc:
                    log.warning("timeout notify %s failed: %s", order["id"], exc)
                expired.append(order["id"])
        return expired

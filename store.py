"""SQLite-хранилище, общее для двух ботов «Сигма».

Заказы создаёт клиентский бот, а диспетчерский бот публикует их в чат,
где водители нажимают «Принять». Принятие — атомарный UPDATE, поэтому
два водителя не могут взять один заказ.
"""

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    chat_id     INTEGER PRIMARY KEY,
    name        TEXT,
    username    TEXT,
    phone       TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS couriers (
    chat_id     INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    phone       TEXT,
    username    TEXT,
    car         TEXT,
    color       TEXT,
    plate       TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id         INTEGER NOT NULL,
    client_name       TEXT,
    client_username   TEXT,
    client_phone      TEXT,
    from_addr         TEXT NOT NULL,
    to_addr           TEXT NOT NULL,
    comment           TEXT,
    status            TEXT NOT NULL,
    courier_id        INTEGER,
    courier_name      TEXT,
    courier_phone     TEXT,
    courier_car       TEXT,
    courier_color     TEXT,
    courier_plate     TEXT,
    group_chat_id     INTEGER,
    group_message_id  INTEGER,
    client_message_id INTEGER,
    created_at        INTEGER NOT NULL,
    accepted_at       INTEGER,
    done_at           INTEGER,
    cancelled_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_client ON orders (client_id, status);

CREATE TABLE IF NOT EXISTS order_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL,
    status     TEXT NOT NULL,
    actor_name TEXT,
    actor_id   INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_order ON order_events (order_id);

CREATE TABLE IF NOT EXISTS sessions (
    bot         TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    state       TEXT,
    data        TEXT,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (bot, chat_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS admins (
    chat_id    INTEGER PRIMARY KEY,
    name       TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS forward_sources (
    chat_id    INTEGER PRIMARY KEY,
    title      TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS forward_targets (
    chat_id    INTEGER PRIMARY KEY,
    title      TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS forwarded_messages (
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    dedupe_key  TEXT,
    sent_at     INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_forwarded_key
    ON forwarded_messages (dedupe_key, sent_at);

CREATE TABLE IF NOT EXISTS chats_seen (
    chat_id    INTEGER PRIMARY KEY,
    title      TEXT,
    kind       TEXT,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Досоздать колонки в уже работающей базе."""
        courier_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(couriers)")}
        if "color" not in courier_cols:
            self.db.execute("ALTER TABLE couriers ADD COLUMN color TEXT")
        order_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(orders)")}
        for col in ("courier_car", "courier_color", "courier_plate"):
            if col not in order_cols:
                self.db.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _dict(row):
        return dict(row) if row is not None else None

    def _one(self, sql, args=()):
        with self.lock:
            return self._dict(self.db.execute(sql, args).fetchone())

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ----------------------------------------------------------------- client
    def ensure_client(self, chat_id, name=None, username=None, phone=None):
        now = int(time.time())
        with self.lock:
            self.db.execute(
                "INSERT INTO clients (chat_id, name, username, phone, created_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "name=COALESCE(excluded.name, clients.name), "
                "username=COALESCE(excluded.username, clients.username), "
                "phone=COALESCE(excluded.phone, clients.phone)",
                (chat_id, name, username, phone, now),
            )

    def get_client(self, chat_id):
        return self._one("SELECT * FROM clients WHERE chat_id=?", (chat_id,))

    # ---------------------------------------------------------------- courier
    def get_courier(self, chat_id):
        return self._one("SELECT * FROM couriers WHERE chat_id=?", (chat_id,))

    def create_courier(self, chat_id, full_name, phone=None, car=None, color=None,
                       plate=None, username=None):
        now = int(time.time())
        with self.lock:
            self.db.execute(
                "INSERT INTO couriers "
                "(chat_id, full_name, phone, username, car, color, plate, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'active', ?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "full_name=excluded.full_name, phone=excluded.phone, "
                "username=COALESCE(excluded.username, couriers.username), "
                "car=excluded.car, color=excluded.color, plate=excluded.plate, "
                "status='active', updated_at=excluded.updated_at",
                (chat_id, full_name, phone, username, car, color, plate, now, now),
            )
        return self.get_courier(chat_id)

    def set_courier_status(self, chat_id, status):
        with self.lock:
            self.db.execute(
                "UPDATE couriers SET status=?, updated_at=? WHERE chat_id=?",
                (status, int(time.time()), chat_id),
            )

    def courier_count(self, only_active=False):
        sql = "SELECT COUNT(*) AS n FROM couriers"
        if only_active:
            sql += " WHERE status='active'"
        row = self._one(sql)
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ orders
    def create_order(
        self,
        client_id,
        from_addr,
        to_addr,
        client_name=None,
        client_username=None,
        client_phone=None,
        comment=None,
        client_message_id=None,
    ):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO orders "
                "(client_id, client_name, client_username, client_phone, from_addr, to_addr, "
                "comment, status, client_message_id, created_at) "
                "VALUES (?,?,?,?,?,?,?, 'new', ?,?)",
                (
                    client_id,
                    client_name,
                    client_username,
                    client_phone,
                    from_addr,
                    to_addr,
                    comment,
                    client_message_id,
                    now,
                ),
            )
            order_id = cur.lastrowid
            self._log_event(order_id, "new", client_name, client_id)
        return self.get_order(order_id)

    def get_order(self, order_id):
        return self._one("SELECT * FROM orders WHERE id=?", (order_id,))

    def set_group_message(self, order_id, chat_id, message_id):
        with self.lock:
            self.db.execute(
                "UPDATE orders SET group_chat_id=?, group_message_id=?, status='searching' "
                "WHERE id=? AND status='new'",
                (chat_id, message_id, order_id),
            )
            self._log_event(order_id, "searching", None, None)

    def set_client_message(self, order_id, message_id):
        with self.lock:
            self.db.execute(
                "UPDATE orders SET client_message_id=? WHERE id=?",
                (message_id, order_id),
            )

    def accept_order(self, order_id, courier):
        """Атомарно принять заказ. Возвращает заказ или None, если уже заняли.

        Вся защита от гонки — условный UPDATE: меняется ровно одна строка.
        """
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = self.db.execute(
                    "UPDATE orders SET status='accepted', courier_id=?, courier_name=?, "
                    "courier_phone=?, courier_car=?, courier_color=?, courier_plate=?, "
                    "accepted_at=? "
                    "WHERE id=? AND status IN ('new','searching')",
                    (
                        courier["chat_id"],
                        courier["full_name"],
                        courier.get("phone"),
                        courier.get("car"),
                        courier.get("color"),
                        courier.get("plate"),
                        now,
                        order_id,
                    ),
                )
                if cur.rowcount != 1:
                    self.db.execute("ROLLBACK")
                    return None
                row = self.db.execute(
                    "SELECT * FROM orders WHERE id=?", (order_id,)
                ).fetchone()
                self._log_event(
                    order_id, "accepted", courier["full_name"], courier["chat_id"]
                )
                self.db.execute("COMMIT")
                return self._dict(row)
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def set_order_status(self, order_id, status, courier_id=None, actor_name=None, actor_id=None):
        """Сменить статус заказа (забрал / доставил / отменил).

        Допустимые переходы проверяются в SQL, чтобы нельзя было, например,
        «доставить» уже отменённый заказ.
        """
        now = int(time.time())
        allowed_from = {
            "on_way": ("accepted",),
            "done": ("accepted", "on_way"),
            "cancelled": ("new", "searching", "accepted", "on_way"),
        }
        from_statuses = allowed_from.get(status)
        if not from_statuses:
            return False

        fields = ["status=?"]
        args = [status]
        if status == "done":
            fields.append("done_at=?")
            args.append(now)
        if status == "cancelled":
            fields.append("cancelled_at=?")
            args.append(now)

        placeholders = ",".join("?" for _ in from_statuses)
        where = f"id=? AND status IN ({placeholders})"
        args.append(order_id)
        args.extend(from_statuses)
        if courier_id is not None:
            where += " AND courier_id=?"
            args.append(courier_id)
        with self.lock:
            cur = self.db.execute(
                f"UPDATE orders SET {', '.join(fields)} WHERE {where}", args
            )
            if cur.rowcount == 1:
                self._log_event(order_id, status, actor_name, actor_id)
                return True
            return False

    def active_order_for_client(self, client_id):
        return self._one(
            "SELECT * FROM orders WHERE client_id=? AND status IN "
            "('new','searching','accepted','on_way') ORDER BY id DESC LIMIT 1",
            (client_id,),
        )

    def last_order_for_client(self, client_id):
        return self._one(
            "SELECT * FROM orders WHERE client_id=? ORDER BY id DESC LIMIT 1",
            (client_id,),
        )

    def active_order_for_courier(self, courier_id):
        return self._one(
            "SELECT * FROM orders WHERE courier_id=? AND status IN "
            "('accepted','on_way') ORDER BY id DESC LIMIT 1",
            (courier_id,),
        )

    def orders_for_courier_today(self, courier_id):
        start = int(time.time()) - 24 * 3600
        return self._all(
            "SELECT * FROM orders WHERE courier_id=? AND status='done' AND done_at>=? "
            "ORDER BY id DESC",
            (courier_id, start),
        )

    def stale_searching_orders(self, max_age_seconds):
        cutoff = int(time.time()) - int(max_age_seconds)
        return self._all(
            "SELECT * FROM orders WHERE status IN ('new','searching') AND created_at<=? "
            "ORDER BY id",
            (cutoff,),
        )

    def unpublished_orders(self, min_age_seconds=5):
        """Заказы, которые ещё не ушли в чат (например, бот не был в группе)."""
        cutoff = int(time.time()) - int(min_age_seconds)
        return self._all(
            "SELECT * FROM orders WHERE status='new' AND created_at<=? ORDER BY id",
            (cutoff,),
        )

    # -------------------------------------------------------------- statistics
    def _day_start(self, days_ago=0):
        now = time.localtime()
        midnight = time.mktime(
            (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)
        )
        return int(midnight - days_ago * 86400)

    def courier_stats(self, courier_id):
        """Статистика одного водителя."""
        total = self._one(
            "SELECT COUNT(*) AS n FROM orders WHERE courier_id=?", (courier_id,)
        )
        done = self._one(
            "SELECT COUNT(*) AS n FROM orders WHERE courier_id=? AND status='done'",
            (courier_id,),
        )
        active = self._one(
            "SELECT COUNT(*) AS n FROM orders WHERE courier_id=? "
            "AND status IN ('accepted','on_way')",
            (courier_id,),
        )
        cancelled = self._one(
            "SELECT COUNT(*) AS n FROM orders WHERE courier_id=? AND status='cancelled'",
            (courier_id,),
        )
        today_start = self._day_start()
        week_start = self._day_start(6)
        today_total = self._one(
            "SELECT COUNT(*) AS n FROM orders WHERE courier_id=? AND accepted_at>=?",
            (courier_id, today_start),
        )
        today_done = self._one(
            "SELECT COUNT(*) AS n FROM orders WHERE courier_id=? AND status='done' "
            "AND done_at>=?",
            (courier_id, today_start),
        )
        week_done = self._one(
            "SELECT COUNT(*) AS n FROM orders WHERE courier_id=? AND status='done' "
            "AND done_at>=?",
            (courier_id, week_start),
        )
        avg = self._one(
            "SELECT AVG(accepted_at - created_at) AS a FROM orders "
            "WHERE courier_id=? AND accepted_at IS NOT NULL",
            (courier_id,),
        )
        return {
            "total": int(total["n"]),
            "done": int(done["n"]),
            "active": int(active["n"]),
            "cancelled": int(cancelled["n"]),
            "today_total": int(today_total["n"]),
            "today_done": int(today_done["n"]),
            "week_done": int(week_done["n"]),
            "avg_accept_seconds": avg["a"],
        }

    def admin_stats(self):
        """Общая статистика для админки."""
        def count(where="1=1", args=()):
            row = self._one(f"SELECT COUNT(*) AS n FROM orders WHERE {where}", args)
            return int(row["n"])

        today = self._day_start()
        avg = self._one(
            "SELECT AVG(accepted_at - created_at) AS a FROM orders "
            "WHERE accepted_at IS NOT NULL"
        )
        return {
            "orders_total": count(),
            "searching": count("status IN ('new','searching')"),
            "in_progress": count("status IN ('accepted','on_way')"),
            "done": count("status='done'"),
            "cancelled": count("status='cancelled'"),
            "today_total": count("created_at>=?", (today,)),
            "today_done": count("status='done' AND done_at>=?", (today,)),
            "today_cancelled": count("status='cancelled' AND cancelled_at>=?", (today,)),
            "couriers_total": self.courier_count(),
            "couriers_active": self.courier_count(only_active=True),
            "avg_accept_seconds": avg["a"],
        }

    def couriers_with_stats(self):
        rows = self._all(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM orders o WHERE o.courier_id=c.chat_id "
            " AND o.status='done') AS done_count, "
            "(SELECT COUNT(*) FROM orders o WHERE o.courier_id=c.chat_id "
            " AND o.status='cancelled') AS cancel_count "
            "FROM couriers c ORDER BY done_count DESC, c.full_name"
        )
        return rows

    def recent_orders(self, limit=10):
        return self._all(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (int(limit),)
        )

    def _log_event(self, order_id, status, actor_name, actor_id):
        self.db.execute(
            "INSERT INTO order_events (order_id, status, actor_name, actor_id, created_at) "
            "VALUES (?,?,?,?,?)",
            (order_id, status, actor_name, actor_id, int(time.time())),
        )

    def order_events(self, order_id):
        return self._all(
            "SELECT * FROM order_events WHERE order_id=? ORDER BY id", (order_id,)
        )

    # ------------------------------------------------------------------ meta
    def meta_get(self, key, default=None):
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def meta_set(self, key, value):
        with self.lock:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # ------------------------------------------------------------- seen chats
    def remember_chat(self, chat_id, title=None, kind=None):
        """Запомнить группу/канал, где бот был замечен: пригодится для рассылки."""
        now = int(time.time())
        with self.lock:
            self.db.execute(
                "INSERT INTO chats_seen (chat_id, title, kind, first_seen, last_seen) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "title=COALESCE(excluded.title, chats_seen.title), "
                "kind=COALESCE(excluded.kind, chats_seen.kind), "
                "last_seen=excluded.last_seen",
                (chat_id, title, kind, now, now),
            )

    def seen_chats(self, only_groups=True):
        sql = "SELECT * FROM chats_seen"
        if only_groups:
            sql += " WHERE kind IN ('group','supergroup','channel')"
        sql += " ORDER BY last_seen DESC"
        return self._all(sql)

    # ----------------------------------------------------------------- sessions
    def get_session(self, bot, chat_id):
        row = self._one(
            "SELECT * FROM sessions WHERE bot=? AND chat_id=?", (bot, chat_id)
        )
        if not row:
            return None
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            data = {}
        return {"state": row["state"], "data": data}

    def set_session(self, bot, chat_id, state, data=None):
        with self.lock:
            self.db.execute(
                "INSERT INTO sessions (bot, chat_id, state, data, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(bot, chat_id) DO UPDATE SET "
                "state=excluded.state, data=excluded.data, updated_at=excluded.updated_at",
                (bot, chat_id, state, json.dumps(data or {}, ensure_ascii=False), int(time.time())),
            )

    def clear_session(self, bot, chat_id):
        with self.lock:
            self.db.execute(
                "DELETE FROM sessions WHERE bot=? AND chat_id=?", (bot, chat_id)
            )

    # ------------------------------------------------------------------- admins
    def is_admin(self, chat_id):
        return (
            self._one("SELECT 1 AS x FROM admins WHERE chat_id=?", (chat_id,)) is not None
        )

    def add_admin(self, chat_id, name=None):
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO admins (chat_id, name, created_at) VALUES (?,?,?)",
                (chat_id, name, int(time.time())),
            )

    # -------------------------------------------------------- forward sources
    def add_forward_source(self, chat_id, title=None):
        with self.lock:
            self.db.execute(
                "INSERT INTO forward_sources (chat_id, title, enabled, created_at) "
                "VALUES (?,?,1,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, enabled=1",
                (chat_id, title, int(time.time())),
            )
        return True

    def remove_forward_source(self, chat_id):
        with self.lock:
            cur = self.db.execute("DELETE FROM forward_sources WHERE chat_id=?", (chat_id,))
        return cur.rowcount > 0

    def forward_sources(self, only_enabled=False):
        sql = "SELECT * FROM forward_sources"
        if only_enabled:
            sql += " WHERE enabled=1"
        return self._all(sql + " ORDER BY created_at")

    def is_forward_source(self, chat_id):
        return (
            self._one(
                "SELECT 1 AS x FROM forward_sources WHERE chat_id=? AND enabled=1",
                (chat_id,),
            )
            is not None
        )

    # -------------------------------------------------------- forward targets
    def add_forward_target(self, chat_id, title=None):
        with self.lock:
            self.db.execute(
                "INSERT INTO forward_targets (chat_id, title, enabled, created_at) "
                "VALUES (?,?,1,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, enabled=1",
                (chat_id, title, int(time.time())),
            )
        return True

    def remove_forward_target(self, chat_id):
        with self.lock:
            cur = self.db.execute("DELETE FROM forward_targets WHERE chat_id=?", (chat_id,))
        return cur.rowcount > 0

    def forward_targets(self, only_enabled=False):
        sql = "SELECT * FROM forward_targets"
        if only_enabled:
            sql += " WHERE enabled=1"
        return self._all(sql + " ORDER BY created_at")

    def is_forward_target(self, chat_id):
        return (
            self._one(
                "SELECT 1 AS x FROM forward_targets WHERE chat_id=? AND enabled=1",
                (chat_id,),
            )
            is not None
        )

    def mark_forwarded(self, chat_id, message_id, dedupe_key=""):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO forwarded_messages "
                "(chat_id, message_id, dedupe_key, sent_at) VALUES (?,?,?,?)",
                (chat_id, message_id, dedupe_key, int(time.time())),
            )

    def already_forwarded(self, chat_id, message_id):
        return (
            self._one(
                "SELECT 1 AS x FROM forwarded_messages WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
            is not None
        )

    def is_duplicate_text(self, dedupe_key, ttl_seconds):
        if not dedupe_key:
            return False
        since = int(time.time()) - int(ttl_seconds)
        return (
            self._one(
                "SELECT 1 AS x FROM forwarded_messages "
                "WHERE dedupe_key=? AND dedupe_key<>'' AND sent_at>=?",
                (dedupe_key, since),
            )
            is not None
        )

    def prune_forwarded(self, keep_seconds=7 * 24 * 3600):
        with self.lock:
            self.db.execute(
                "DELETE FROM forwarded_messages WHERE sent_at < ?",
                (int(time.time()) - int(keep_seconds),),
            )

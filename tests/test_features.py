import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot import ClientBot, GroupBot  # noqa: E402
from core import ActionGuard, SigmaService, group_card_text  # noqa: E402
from store import Store  # noqa: E402
from test_handlers import FakeAPI, callback, text_message  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "sigma.db"))
        self.client_api = FakeAPI()
        self.group_api = FakeAPI()
        self.service = SigmaService(self.store, self.client_api, self.group_api, -100500)
        self.stop = threading.Event()
        self.client = ClientBot(
            self.client_api, self.store, self.service, self.stop, admin_code="SECRET"
        )
        self.group = GroupBot(
            self.group_api, self.store, self.service, self.stop,
            admin_code="SECRET", order_ttl_minutes=30,
        )

    def make_order(self):
        return self.store.create_order(
            client_id=555, client_name="Аня", client_username="anya",
            client_phone="+79990000000", from_addr="A", to_addr="B",
        )


class NoPriceTests(Base):
    def test_card_has_no_price(self):
        order = self.make_order()
        text = group_card_text(order)
        self.assertNotIn("₽", text)
        self.assertNotIn("Стоимость", text)
        self.assertIn("Куда", text)
        self.assertIn("Клиент", text)

    def test_accept_shows_car_in_group_and_client(self):
        order = self.make_order()
        self.service.publish(order["id"])
        courier = self.store.create_courier(
            777, "Петров Пётр", phone="+79991112233",
            car="Kia Rio", color="белый", plate="А123ВС77",
        )
        self.service.accept(order["id"], courier)
        fresh = self.store.get_order(order["id"])
        self.assertEqual(fresh["courier_car"], "Kia Rio")
        self.assertEqual(fresh["courier_plate"], "А123ВС77")

        group_text = [
            m["text"] for m in self.group_api.messages.values()
            if m["chat_id"] == -100500
        ][-1]
        self.assertIn("Kia Rio, белый, А123ВС77", group_text)

        client_text = [
            m["text"] for m in self.client_api.messages.values()
            if m["chat_id"] == 555
        ][-1]
        self.assertIn("Kia Rio, белый, А123ВС77", client_text)


class StatsTests(Base):
    def test_courier_stats_counts(self):
        courier = self.store.create_courier(777, "Петров Пётр", phone="+7999")
        for _ in range(2):
            order = self.make_order()
            self.service.publish(order["id"])
            self.service.accept(order["id"], courier)
            self.service.change_status(order["id"], "done", courier_id=777)
        stats = self.store.courier_stats(777)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["done"], 2)
        self.assertEqual(stats["today_done"], 2)

    def test_admin_stats_totals(self):
        courier = self.store.create_courier(777, "Петров")
        order = self.make_order()
        self.service.publish(order["id"])
        self.service.accept(order["id"], courier)
        d2 = self.make_order()
        self.service.publish(d2["id"])
        self.service.change_status(d2["id"], "cancelled")
        stats = self.store.admin_stats()
        self.assertEqual(stats["orders_total"], 2)
        self.assertEqual(stats["in_progress"], 1)
        self.assertEqual(stats["cancelled"], 1)
        self.assertEqual(stats["couriers_total"], 1)

    def test_admin_panel_shows_stats(self):
        self.store.add_admin(100)
        self.client.show_admin_panel(100)
        texts = [m["text"] for m in self.client_api.messages.values()]
        self.assertTrue(any("Админка СИГМА" in t for t in texts))

    def test_admin_login_with_code(self):
        self.client.handle_message(text_message(100, "/admin"))
        self.client.handle_message(text_message(100, "SECRET"))
        texts = [m["text"] for m in self.client_api.messages.values()]
        self.assertTrue(any("Админка СИГМА" in t for t in texts))
        self.assertTrue(self.store.is_admin(100))

    def test_courier_menu_has_stats(self):
        self.store.create_courier(777, "Петров Пётр", phone="+7999")
        self.group.handle_message(text_message(777, "/me"))
        buttons = [
            b["text"]
            for m in self.group_api.messages.values()
            for row in (m["reply_markup"] or {}).get("inline_keyboard", [])
            for b in row
        ]
        self.assertIn("📊 Подробная статистика", buttons)


class AntiDoubleClickTests(Base):
    def test_guard_blocks_repeat(self):
        guard = ActionGuard(ttl=5)
        self.assertTrue(guard.claim("k"))
        self.assertFalse(guard.claim("k"))
        time.sleep(0.01)
        self.assertFalse(guard.claim("k"))

    def test_second_click_on_accept_is_harmless(self):
        order = self.make_order()
        self.service.publish(order["id"])
        self.store.create_courier(777, "Петров", phone="+7999")
        for _ in range(3):
            self.group.handle_callback(
                callback(-100500, f"c:accept:{order['id']}", user_id=777, chat_type="supergroup")
            )
        self.assertEqual(self.store.get_order(order["id"])["status"], "accepted")
        answers = [a[0] for a in self.group_api.answered]
        self.assertTrue(any("Уже обрабатывается" in (a or "") for a in answers))

    def test_double_send_creates_one_order(self):
        cid = 555
        self.client.handle_message(text_message(cid, "/start"))
        self.client.handle_callback(callback(cid, "u:new"))
        self.client.handle_message(text_message(cid, "ул. Ленина 1"))
        self.client.handle_message(text_message(cid, "ул. Мира 5"))
        self.client.handle_message(text_message(cid, "-"))
        self.client.handle_message(text_message(cid, "-"))
        self.client.handle_callback(callback(cid, "u:send"))
        self.client.handle_callback(callback(cid, "u:send"))
        orders = [
            o for o in self.store.recent_orders(10) if o["client_id"] == cid
        ]
        self.assertEqual(len(orders), 1)


class AutoExpireTests(Base):
    def test_stale_order_cancelled(self):
        order = self.make_order()
        self.service.publish(order["id"])
        # искусственно состарим заказ
        self.store.db.execute(
            "UPDATE orders SET created_at = created_at - 7200 WHERE id=?", (order["id"],)
        )
        expired = self.service.expire_stale(30 * 60)
        self.assertEqual(expired, [order["id"]])
        self.assertEqual(self.store.get_order(order["id"])["status"], "cancelled")

    def test_fresh_order_not_cancelled(self):
        order = self.make_order()
        self.service.publish(order["id"])
        self.assertEqual(self.service.expire_stale(30 * 60), [])
        self.assertEqual(self.store.get_order(order["id"])["status"], "searching")


if __name__ == "__main__":
    unittest.main()

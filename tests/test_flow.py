import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import SigmaService, client_status_text, group_card_text, group_keyboard  # noqa: E402
from store import Store  # noqa: E402


class FakeAPI:
    def __init__(self):
        self.messages = {}
        self.deleted = set()
        self._next_id = 1
        self._lock = threading.Lock()

    def send_message(self, chat_id, text, **kwargs):
        with self._lock:
            message_id = self._next_id
            self._next_id += 1
            self.messages[message_id] = {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": kwargs.get("reply_markup"),
            }
        return {"message_id": message_id, "chat": {"id": chat_id}}

    def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.messages[message_id]["text"] = text
        self.messages[message_id]["reply_markup"] = kwargs.get("reply_markup")
        return True

    def delete_message(self, chat_id, message_id):
        self.deleted.add(message_id)
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "sigma.db"))
        self.client_api = FakeAPI()
        self.group_api = FakeAPI()
        self.service = SigmaService(self.store, self.client_api, self.group_api, -100123)
        self.store.ensure_client(555, "Аня", "anya", "+79990000000")

    def make_order(self, to_addr="ул. Мира 5"):
        return self.store.create_order(
            client_id=555,
            client_name="Аня",
            client_username="anya",
            client_phone="+79990000000",
            from_addr="ул. Ленина 1",
            to_addr=to_addr,
            comment="подъезд 2",
        )


class PublishTests(Base):
    def test_publish_sends_card_with_accept_button(self):
        order = self.make_order()
        message_id = self.service.publish(order["id"])
        self.assertIsNotNone(message_id)
        card = self.group_api.messages[message_id]
        self.assertEqual(card["chat_id"], -100123)
        self.assertIn("НОВЫЙ ЗАКАЗ", card["text"])
        self.assertIn("Принять заказ", card["reply_markup"]["inline_keyboard"][0][0]["text"])
        self.assertEqual(self.store.get_order(order["id"])["status"], "searching")

    def test_publish_without_group_chat_id_is_noop(self):
        self.service.group_chat_id = None
        order = self.make_order()
        self.assertIsNone(self.service.publish(order["id"]))


class AcceptTests(Base):
    def test_accept_updates_group_card_and_client(self):
        order = self.make_order()
        self.service.publish(order["id"])
        courier = self.store.create_courier(777, "Петров Пётр", phone="+79991112233")
        accepted = self.service.accept(order["id"], courier)
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["courier_name"], "Петров Пётр")

        group_msg = self.group_api.messages[accepted["group_message_id"]]
        self.assertIn("Принял:", group_msg["text"])
        self.assertIn("Петров Пётр", group_msg["text"])
        # кнопки следующего шага
        labels = [
            b["text"]
            for row in group_msg["reply_markup"]["inline_keyboard"]
            for b in row
        ]
        self.assertIn("🚗 Выехал к клиенту", labels)

        # клиенту ушло уведомление с ФИО водителя
        client_texts = [
            m["text"] for m in self.client_api.messages.values() if m["chat_id"] == 555
        ]
        self.assertTrue(any("Петров Пётр" in t for t in client_texts))

    def test_second_courier_cannot_accept(self):
        order = self.make_order()
        self.service.publish(order["id"])
        first = self.store.create_courier(777, "Первый", phone="+79991112233")
        second = self.store.create_courier(778, "Второй", phone="+79994445566")
        self.assertIsNotNone(self.service.accept(order["id"], first))
        self.assertIsNone(self.service.accept(order["id"], second))
        self.assertEqual(self.store.get_order(order["id"])["courier_id"], 777)

    def test_accept_race_only_one_wins(self):
        order = self.make_order()
        self.service.publish(order["id"])
        couriers = [self.store.create_courier(900 + i, f"Водитель {i}") for i in range(8)]
        results = []
        lock = threading.Lock()

        def try_accept(courier):
            got = self.store.accept_order(order["id"], courier)
            with lock:
                results.append(got is not None)

        threads = [threading.Thread(target=try_accept, args=(c,)) for c in couriers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)


class StatusTests(Base):
    def test_full_lifecycle(self):
        order = self.make_order()
        self.service.publish(order["id"])
        courier = self.store.create_courier(777, "Петров Пётр", phone="+79991112233")
        self.service.accept(order["id"], courier)

        self.assertTrue(
            self.service.change_status(order["id"], "on_way", courier_id=777, actor_name="Петров")
        )
        self.assertEqual(self.store.get_order(order["id"])["status"], "on_way")

        self.assertTrue(
            self.service.change_status(order["id"], "done", courier_id=777, actor_name="Петров")
        )
        done = self.store.get_order(order["id"])
        self.assertEqual(done["status"], "done")
        self.assertIsNotNone(done["done_at"])
        self.assertIsNone(self.store.active_order_for_client(555))
        self.assertIsNone(self.store.active_order_for_courier(777))

    def test_wrong_courier_cannot_change_status(self):
        order = self.make_order()
        self.service.publish(order["id"])
        courier = self.store.create_courier(777, "Петров", phone="+7999")
        self.service.accept(order["id"], courier)
        self.assertFalse(self.service.change_status(order["id"], "done", courier_id=999))
        self.assertEqual(self.store.get_order(order["id"])["status"], "accepted")

    def test_client_cancel_only_before_accept(self):
        order = self.make_order()
        self.service.publish(order["id"])
        self.assertTrue(self.service.change_status(order["id"], "cancelled", actor_name="Клиент"))
        courier = self.store.create_courier(777, "Петров")
        self.assertIsNone(self.service.accept(order["id"], courier))


class FormatTests(Base):
    def test_group_card_contains_addresses_and_accepted_block(self):
        order = self.make_order()
        text = group_card_text(order)
        self.assertIn("ул. Ленина 1", text)
        self.assertIn("ул. Мира 5", text)
        self.assertNotIn("Принял:", text)

    def test_keyboards_by_status(self):
        order = self.make_order()
        order["status"] = "searching"
        self.assertTrue(group_keyboard(order)["inline_keyboard"])
        order["status"] = "done"
        self.assertEqual(group_keyboard(order), {"inline_keyboard": []})

    def test_client_text_shows_courier_name(self):
        order = self.make_order()
        order.update(
            status="accepted",
            courier_name="Петров Пётр",
            courier_phone="+79991112233",
            courier_id=777,
        )
        text = client_status_text(order)
        self.assertIn("Петров Пётр", text)
        self.assertIn("+79991112233", text)


if __name__ == "__main__":
    unittest.main()

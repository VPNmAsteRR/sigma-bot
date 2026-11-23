import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot import ClientBot, GroupBot  # noqa: E402
from core import SigmaService  # noqa: E402
from store import Store  # noqa: E402


class FakeAPI:
    def __init__(self):
        self.messages = {}
        self.answered = []
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
        return True

    def answer_callback_query(self, callback_id, text=None, show_alert=False):
        self.answered.append((text, show_alert))
        return True


def text_message(chat_id, text, user_id=None, chat_type="private"):
    return {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id or chat_id, "first_name": "Тест", "username": "test"},
        "text": text,
    }


def callback(chat_id, data, user_id=None, message_id=1, chat_type="private"):
    return {
        "id": "cb",
        "data": data,
        "from": {"id": user_id or chat_id, "first_name": "Тест"},
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": chat_type},
        },
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "sigma.db"))
        self.client_api = FakeAPI()
        self.group_api = FakeAPI()
        self.service = SigmaService(self.store, self.client_api, self.group_api, -100500)
        self.stop = threading.Event()
        self.client = ClientBot(self.client_api, self.store, self.service, self.stop)
        self.group = GroupBot(self.group_api, self.store, self.service, self.stop)


class ClientFlowTests(Base):
    def test_order_flow_end_to_end(self):
        cid = 555
        self.client.handle_message(text_message(cid, "/start"))
        self.client.handle_callback(callback(cid, "u:new"))
        self.client.handle_message(text_message(cid, "ул. Ленина 1"))
        self.client.handle_message(text_message(cid, "ул. Мира 5"))
        self.client.handle_message(text_message(cid, "-"))
        self.client.handle_message(text_message(cid, "+79991234567"))
        self.client.handle_callback(callback(cid, "u:send"))

        order = self.store.active_order_for_client(cid)
        self.assertIsNotNone(order)
        self.assertEqual(order["to_addr"], "ул. Мира 5")
        self.assertEqual(order["status"], "searching")
        # заказ опубликован в чат водителей
        publish_bot = self.group_api.messages
        self.assertTrue(any(m["chat_id"] == -100500 for m in publish_bot.values()))

    def test_phone_service_for_group_bot_registration(self):
        wid = 777
        self.group.handle_message(text_message(wid, "/start"))
        self.group.handle_message(text_message(wid, "Петров Пётр"))
        self.group.handle_message(text_message(wid, "Kia Rio"))
        self.group.handle_message(text_message(wid, "белый"))
        self.group.handle_message(text_message(wid, "а123вс77"))
        self.group.handle_message(text_message(wid, "+79991112233"))
        courier = self.store.get_courier(wid)
        self.assertIsNotNone(courier)
        self.assertEqual(courier["full_name"], "Петров Пётр")
        self.assertEqual(courier["phone"], "+79991112233")
        self.assertEqual(courier["car"], "Kia Rio")
        self.assertEqual(courier["color"], "белый")
        self.assertEqual(courier["plate"], "А123ВС77")

    def test_registration_requires_car_fields(self):
        wid = 778
        self.group.handle_message(text_message(wid, "/start"))
        self.group.handle_message(text_message(wid, "Иванов Иван"))
        self.group.handle_message(text_message(wid, ""))  # пустая марка
        self.assertIsNone(self.store.get_courier(wid))
        state = self.store.get_session("group", wid)
        self.assertEqual(state["state"], "car")

    def test_accept_via_group_callback(self):
        cid = 555
        self.store.ensure_client(cid, "Аня", "anya", "+79990000000")
        order = self.store.create_order(
            client_id=cid, client_name="Аня", from_addr="A", to_addr="B"
        )
        self.service.publish(order["id"])
        self.store.create_courier(777, "Петров Пётр", phone="+7999")

        self.group.handle_callback(
            callback(-100500, f"c:accept:{order['id']}", user_id=777, chat_type="supergroup")
        )
        fresh = self.store.get_order(order["id"])
        self.assertEqual(fresh["status"], "accepted")
        self.assertEqual(fresh["courier_id"], 777)

        self.group.handle_callback(
            callback(-100500, f"c:done:{order['id']}", user_id=777, chat_type="supergroup")
        )
        self.assertEqual(self.store.get_order(order["id"])["status"], "done")

    def test_unregistered_cannot_accept(self):
        cid = 555
        order = self.store.create_order(client_id=cid, client_name="A", from_addr="A", to_addr="B")
        self.service.publish(order["id"])
        self.group.handle_callback(
            callback(-100500, f"c:accept:{order['id']}", user_id=999, chat_type="supergroup")
        )
        self.assertEqual(self.store.get_order(order["id"])["status"], "searching")

    def test_forward_command_requires_reply(self):
        self.group.handle_message(text_message(-100500, "/forward", chat_type="supergroup"))
        texts = [m["text"] for m in self.group_api.messages.values()]
        self.assertTrue(any("Ответьте командой" in t for t in texts))

    def test_cannot_reopen_done_order(self):
        cid = 555
        order = self.store.create_order(client_id=cid, client_name="A", from_addr="A", to_addr="B")
        self.service.publish(order["id"])
        courier = self.store.create_courier(777, "Петров")
        self.service.accept(order["id"], courier)
        self.assertTrue(self.service.change_status(order["id"], "done", courier_id=777))
        self.assertFalse(self.service.change_status(order["id"], "on_way", courier_id=777))
        self.assertEqual(self.store.get_order(order["id"])["status"], "done")


if __name__ == "__main__":
    unittest.main()

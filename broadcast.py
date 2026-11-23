#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Рассылка поста с кнопкой «Заказать такси» в группы/каналы.

Примеры:

    # одноразово в группу по ID
    python3 broadcast.py --chat -1001234567890 --photo banner.png \
        --text-file post.txt --button "Сделать заказ|https://t.me/TaxiSigmabot"

    # во все группы, где бот уже замечен (/groups в личке бота)
    python3 broadcast.py --all-seen --photo banner.png --text-file post.txt

    # проверка без отправки
    python3 broadcast.py --all-seen --photo banner.png --text-file post.txt --dry-run

Кнопка задаётся как «Текст|https://ссылка» (можно несколько через ``;;``).
Только стандартная библиотека — как и остальной проект.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


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


class Api:
    def __init__(self, token, base="https://api.telegram.org"):
        self.url = f"{base.rstrip('/')}/bot{token}/"

    def _post(self, method, params, timeout=60):
        body = json.dumps(params).encode()
        req = urllib.request.Request(
            self.url + method,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except Exception:
                raise RuntimeError(f"{method}: HTTP {exc.code} {exc.reason}")
        if not payload.get("ok"):
            raise RuntimeError(
                f"{method}: [{payload.get('error_code')}] {payload.get('description')}"
            )
        return payload["result"]

    def post(self, method, params, timeout=60, attempts=3):
        delay = 2
        last = None
        for _ in range(attempts):
            try:
                return self._post(method, params, timeout)
            except Exception as exc:  # noqa: BLE001
                last = exc
                text = str(exc)
                if "429" in text:
                    wait = 5
                    try:
                        wait = int(text.split("retry after ")[1].split(")")[0])
                    except Exception:
                        pass
                    time.sleep(max(1, min(wait, 60)))
                    continue
                if any(code in text for code in ("[500]", "[502]", "[503]", "[504]", "timed out")):
                    time.sleep(delay)
                    delay = min(delay * 2, 20)
                    continue
                raise
        raise last

    def send_photo(self, chat_id, path, caption="", keyboard=None):
        params = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        if keyboard:
            params["reply_markup"] = keyboard
        return self._multipart("sendPhoto", "photo", path, params)

    def send_text(self, chat_id, text, keyboard=None, preview=True):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not preview},
        }
        if keyboard:
            params["reply_markup"] = keyboard
        return self.post("sendMessage", params)

    def _multipart(self, method, field, path, params):
        boundary = "----SigmaBoundary7d91a2"
        parts = []
        for key, value in params.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                f"{value}\r\n".encode("utf-8")
            )
        filename = os.path.basename(path)
        with open(path, "rb") as fh:
            data = fh.read()
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n"
            ).encode("utf-8")
            + data
            + b"\r\n"
        )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")

        req = urllib.request.Request(
            self.url + method,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except Exception:
                raise RuntimeError(f"{method}: HTTP {exc.code} {exc.reason}")
        if not payload.get("ok"):
            raise RuntimeError(
                f"{method}: [{payload.get('error_code')}] {payload.get('description')}"
            )
        return payload["result"]

    def get_chat(self, chat_id):
        return self.post("getChat", {"chat_id": chat_id})


def parse_buttons(raw):
    """'Текст|url;;Текст2|url2' -> [[button]]."""
    if not raw:
        return None
    rows = []
    for chunk in raw.split(";;"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" not in chunk:
            raise SystemExit(f"Кнопка должна быть «Текст|ссылка», получено: {chunk}")
        text, _, url = chunk.partition("|")
        rows.append([{"text": text.strip(), "url": url.strip()}])
    return {"inline_keyboard": rows} if rows else None


def read_text(args):
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return (args.text or "").strip()


def seen_chats(db_path):
    import sqlite3

    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT chat_id, title FROM chats_seen ORDER BY last_seen DESC").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return rows


def main():
    load_env_file(os.path.join(BASE_DIR, ".env"))

    p = argparse.ArgumentParser(description="Рассылка поста с кнопкой в группы/каналы")
    p.add_argument("--chat", action="append", default=[], help="ID чата (можно несколько раз)")
    p.add_argument("--all-seen", action="store_true", help="Все чаты, где бот уже был замечен")
    p.add_argument("--text", default="", help="Текст поста (HTML)")
    p.add_argument("--text-file", help="Файл с текстом поста")
    p.add_argument("--photo", help="Картинка к посту (png/jpg)")
    p.add_argument("--button", default="", help="«Текст|https://ссылка», несколько через ;;")
    p.add_argument("--pin", action="store_true", help="Закрепить пост")
    p.add_argument("--delay", type=float, default=2.0, help="Пауза между чатами, сек")
    p.add_argument("--dry-run", action="store_true", help="Ничего не отправлять, только показать")
    p.add_argument("--data-dir", default=os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data")))
    args = p.parse_args()

    token = os.environ.get("CLIENT_BOT_TOKEN") or os.environ.get("GROUP_BOT_TOKEN")
    if not token:
        raise SystemExit("Нет CLIENT_BOT_TOKEN/GROUP_BOT_TOKEN в .env")
    api = Api(token, os.environ.get("API_BASE", "https://api.telegram.org"))

    caption = read_text(args)
    if not caption:
        raise SystemExit("Пустой текст поста: задайте --text или --text-file")
    keyboard = parse_buttons(args.button)

    targets = []
    for raw in args.chat:
        try:
            targets.append((int(raw), str(raw)))
        except ValueError:
            targets.append((raw, raw))
    if args.all_seen:
        data_dir = args.data_dir
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(BASE_DIR, data_dir)
        targets.extend(seen_chats(os.path.join(data_dir, "sigma.db")))

    # убрать дубли, сохранив порядок
    uniq, seen = [], set()
    for chat_id, title in targets:
        if chat_id in seen:
            continue
        seen.add(chat_id)
        uniq.append((chat_id, title))

    if not uniq:
        raise SystemExit("Не заданы получатели: --chat ID или --all-seen")

    print(f"Получателей: {len(uniq)}")
    ok = failed = 0
    for chat_id, title in uniq:
        label = f"{chat_id} ({title})"
        if args.dry_run:
            print(f"[dry] {label}")
            continue
        try:
            api.get_chat(chat_id)
        except Exception as exc:  # noqa: BLE001
            print(f"✖ {label}: нет доступа — {exc}")
            failed += 1
            continue
        try:
            if args.photo:
                msg = api.send_photo(chat_id, args.photo, caption, keyboard)
            else:
                msg = api.send_text(chat_id, caption, keyboard)
            if args.pin:
                try:
                    api.post(
                        "pinChatMessage",
                        {"chat_id": chat_id, "message_id": msg["message_id"],
                         "disable_notification": True},
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  ⚠ закрепить не вышло: {exc}")
            print(f"✔ {label} → сообщение {msg['message_id']}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"✖ {label}: {exc}")
            failed += 1
        time.sleep(max(0.0, args.delay))

    print(f"\nИтог: отправлено {ok}, ошибок {failed}")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(main())

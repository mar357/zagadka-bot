# -*- coding: utf-8 -*-
"""
Бот для семейной игры "Фото-угадайка".

Механика:
1. Кто угодно пишет боту в личку /загадать
2. Бот просит фото крупным планом -> потом просит написать что это
3. Бот публикует фото в общий чат (без подписи кто загадал)
4. Люди пишут версии ответа прямо в чат обычными сообщениями
5. Бот молча сверяет каждое новое сообщение с сохранённым ответом
    (первое совпадение = победитель, автор загадки исключён из проверки)
6. Раз в день в 20:00 по Москве бот проверяет: если победитель есть -
    объявляет в чат и закрывает загадку. Если никто не угадал - молчит,
    загадка остаётся активной до следующего дня.

Скрипт запускается по крону (через cron-job.org -> workflow_dispatch),
каждый запуск: забирает новые апдейты от Telegram, обрабатывает их,
проверяет не пора ли объявить победителя, сохраняет состояние и выходит.

Все чувствительные данные (кто загадал, ответ, file_id фото, кто угадал)
хранятся в state.json в зашифрованном виде (Fernet), т.к. репозиторий
публичный - без шифрования люд бы открыть файл на GitHub
и увидеть ответ на загадку или telegram id участников.
"""

import os
import re
import json
import traceback
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

import requests
from cryptography.fernet import Fernet, InvalidToken

# ---------- конфиг из секретов ----------

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CHAT_ID = int(os.environ["CHAT_ID"])
ENCRYPT_KEY = os.environ["ENCRYPT_KEY"]

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "state.json"
MATCH_THRESHOLD = 0.8  # допуск на опечатки, но не совсем разные слова

MOSCOW_OFFSET = timedelta(hours=3)  # Москва без перехода на летнее время


# ---------- время ----------

def now_msk():
    return datetime.now(timezone.utc) + MOSCOW_OFFSET


def today_str():
    return now_msk().strftime("%Y-%m-%d")


# ---------- шифрование состояния ----------

def get_fernet():
    try:
        return Fernet(ENCRYPT_KEY.encode())
    except Exception as e:
        raise RuntimeError(
            "ENCRYPT_KEY невалиден - проверь что скопировал ключ полностью в secrets"
        ) from e


def default_state():
    return {
        "last_update_id": 0,
        "active_riddle": None,   # см. структуру ниже
        "pending": {},           # user_id(str) -> {"stage": ..., "photo_file_id": ...}
        "last_announced_date": None,
    }

# active_riddle = {
#     "author_id": int,
#     "answer": str,
#     "photo_file_id": str,
#     "created_date": "YYYY-MM-DD",
#     "winner_id": int | None,
#     "winner_name": str | None,
# }


def load_state(fernet):
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        token = raw.get("data")
        if not token:
            return default_state()
        decrypted = fernet.decrypt(token.encode()).decode()
        return json.loads(decrypted)
    except (InvalidToken, json.JSONDecodeError, KeyError):
        # файл повреждён или ключ не тот - начинаем с чистого состояния,
        # чтобы бот не падал намертво
        return default_state()


def save_state(fernet, state):
    payload = json.dumps(state, ensure_ascii=False).encode()
    token = fernet.encrypt(payload).decode()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"data": token}, f)


# ---------- Telegram API ----------

def tg_call(method, **params):
    resp = requests.post(f"{API}/{method}", json=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data}")
    return data["result"]


def get_updates(offset):
    return tg_call("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])


def send_message(chat_id, text):
    try:
        tg_call("sendMessage", chat_id=chat_id, text=text)
    except Exception:
        print(f"[warn] не смог отправить сообщение в {chat_id}:")
        traceback.print_exc()


def send_photo(chat_id, file_id, caption=None):
    tg_call("sendPhoto", chat_id=chat_id, photo=file_id, caption=caption)


# ---------- нечёткое сравнение ответа ----------

def is_close_match(word, answer):
    word = word.lower().strip()
    answer = answer.lower().strip()
    if not word:
        return False
    if word == answer:
        return True
    return SequenceMatcher(None, word, answer).ratio() >= MATCH_THRESHOLD


def message_matches_answer(text, answer):
    if not text:
        return False
    text_norm = text.lower().strip()
    words = re.findall(r"[а-яё a-z0-9]+", text_norm)
    candidates = [text_norm] + words
    return any(is_close_match(c, answer) for c in candidates)


# ---------- обработка личных сообщений ----------

def handle_private_message(state, user_id, user, text_raw, photo):
    uid = str(user_id)
    text_cmd = text_raw.lower().split("@")[0]  # на случай /команда@BotName

    # --- команды только для админа ---
    if user_id == ADMIN_ID:
        if text_cmd == "/тест_статус":
            reply_status(state, user_id)
            return True
        if text_cmd == "/тест_время":
            force_check_announcement(state)
            return True
        if text_cmd == "/тест_сброс":
            state["active_riddle"] = None
            state["pending"] = {}
            send_message(user_id, "Состояние сброшено")
            return True

    # --- начать новую загадку ---
    if text_cmd == "/загадать":
        if state["active_riddle"]:
            send_message(user_id, "Сейчас уже есть активная загадка, дождись пока её отгадают")
        else:
            state["pending"][uid] = {"stage": "awaiting_photo"}
            send_message(user_id, "Приши мне фото крупным планом")
        return True

    # --- отменить свою загадку ---
    if text_cmd == "/отмена":
        riddle = state["active_riddle"]
        if riddle and (riddle["author_id"] == user_id or user_id == ADMIN_ID):
            state["active_riddle"] = None
            send_message(user_id, "Загадка отменена")
        else:
            send_message(user_id, "Нет активной загадки, которую можно отменить")
        return True

    # --- шаги оформления загадки ---
    pending = state["pending"].get(uid)

    if pending and pending["stage"] == "awaiting_photo" and photo:
        file_id = photo[-1]["file_id"]  # берём самое большое разрешение
        state["pending"][uid] = {"stage": "awaiting_answer", "photo_file_id": file_id}
        send_message(user_id, "Записал! А теперь напиши, что это")
        return True

    if pending and pending["stage"] == "awaiting_answer" and text_raw:
        state["active_riddle"] = {
            "author_id": user_id,
            "answer": text_raw.strip(),
            "photo_file_id": pending["photo_file_id"],
            "created_date": today_str(),
            "winner_id": None,
            "winner_name": None,
        }
        state["pending"].pop(uid, None)
        send_photo(CHAT_ID, state["active_riddle"]["photo_file_id"], caption="Угадайте что это? 🔍")
        send_message(user_id, "Принято! Опубликовал в чат 🔍")
        return True

    return False


def reply_status(state, admin_id):
    riddle = state["active_riddle"]
    if not riddle:
        send_message(admin_id, "[тест] Активной загадки нет")
        return
    has_guess = "да" if riddle.get("winner_id") else "нет"
    text = (
        f"[тест] Активная загадка есть\n"
        f"Создана: {riddle['created_date']}\n"
        f"Кто-то уже угадал: {has_guess}"
    )
    send_message(admin_id, text)


def force_check_announcement(state):
    riddle = state["active_riddle"]
    if riddle and riddle.get("winner_id"):
        text = f"🎉 {riddle['winner_name']} угадал(а)! Ответ был: {riddle['answer']}"
        send_message(CHAT_ID, text)
        send_message(ADMIN_ID, "[тест] Объявление отправлено, загадка закрыта")
        state["active_riddle"] = None
    else:
        send_message(ADMIN_ID, "[тест] Пока никто не угадал, объявления не будет")


# ---------- обработка сообщений в группе ----------

def handle_group_message(state, user, text_raw):
    riddle = state["active_riddle"]
    if not riddle or not text_raw:
        return

    user_id = user.get("id")
    if user_id == riddle["author_id"]:
        return  # автор загадки не может "угадать" сам себя
    if riddle.get("winner_id"):
        return  # победитель уже определён, ждём объявления в 20:00

    if message_matches_answer(text_raw, riddle["answer"]):
        riddle["winner_id"] = user_id
        riddle["winner_name"] = user.get("first_name") or user.get("username") or "Кто-то"


# ---------- ежедневное объявление ----------

def check_daily_announcement(state):
    now = now_msk()
    today = today_str()

    if now.hour != 20:
        return
    if state.get("last_announced_date") == today:
        return

    riddle = state["active_riddle"]
    if riddle and riddle.get("winner_id"):
        text = f"🎉 {riddle['winner_name']} угадал(а)! Ответ был: {riddle['answer']}"
        send_message(CHAT_ID, text)
        state["active_riddle"] = None

    state["last_announced_date"] = today


# ---------- основной цикл ----------

def main():
    fernet = get_fernet()
    state = load_state(fernet)

    updates = get_updates(offset=state["last_update_id"] + 1)
    max_update_id = state["last_update_id"]

    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"])
        msg = upd.get("message")
        if not msg:
            continue

        chat = msg["chat"]
        user = msg.get("from", {})
        user_id = user.get("id")
        text_raw = (msg.get("text") or msg.get("caption") or "").strip()
        photo = msg.get("photo")

        if chat["type"] == "private":
            handle_private_message(state, user_id, user, text_raw, photo)
        elif chat["id"] == CHAT_ID:
            handle_group_message(state, user, text_raw)

    state["last_update_id"] = max_update_id

    check_daily_announcement(state)

    save_state(fernet, state)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("[error] бот упал с ошибкой:")
        traceback.print_exc()
        raise

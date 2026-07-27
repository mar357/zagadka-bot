# -*- coding: utf-8 -*-
"""
Бот для семейной игры "Фото-угадайка".

Механика:
1. Кто угодно пишет боту в личку /загадать
2. Бот просит фото крупным планом -> потом просит написать что это
   (если фото сразу с подписью-названием - публикует сразу, без доп. шага)
3. Бот публикует фото в общий чат (без подписи кто загадал)
4. Люди пишут версии ответа прямо в чат обычными сообщениями
5. Бот молча сверяет каждое новое сообщение с сохранённым ответом
    (первое совпадение = победитель, автор загадки исключён из проверки)
6. Раз в день в 20:00 по Москве бот проверяет: если победитель есть -
    объявляет в чат, начисляет очки автору и победителю и закрывает загадку.
    Если никто не угадал - молчит, загадка остаётся активной до след. дня.
7. По понедельникам - топ недели в чат, 1 числа месяца - топ месяца.
8. Раз в неделю (понедельник) - зашифрованный бэкап state.json в backups/.
9. Раз в день около 17:00 МСК - бот шлёт админу "я жив" в личку.

Скрипт запускается по крону (через cron-job.org -> workflow_dispatch),
каждый запуск: забирает новые апдейты от Telegram, обрабатывает их,
проверяет не пора ли объявить победителя/топ/бэкап/healthcheck,
сохраняет состояние и выходит.

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
BACKUP_DIR = "backups"
MATCH_THRESHOLD = 0.8  # допуск на опечатки, но не совсем разные слова

MOSCOW_OFFSET = timedelta(hours=3)  # Москва без перехода на летнее время


# ---------- время ----------

def now_msk():
    return datetime.now(timezone.utc) + MOSCOW_OFFSET


def today_str():
    return now_msk().strftime("%Y-%m-%d")


def week_key_str(dt):
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def month_key_str(dt):
    return dt.strftime("%Y-%m")


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
        "scores": {},             # user_id(str) -> очки
        "names": {},               # user_id(str) -> последнее известное имя
        "last_weekly_post": None,   # "YYYY-Wnn"
        "last_monthly_post": None,  # "YYYY-MM"
        "last_backup_week": None,   # "YYYY-Wnn"
        "last_healthcheck_date": None,
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
        state = json.loads(decrypted)
        # подстраховка для старых state.json без новых полей
        for key, value in default_state().items():
            state.setdefault(key, value)
        return state
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


# ---------- очки и имена ----------

def remember_name(state, user_id, user):
    if not user_id:
        return
    uid = str(user_id)
    name = user.get("first_name") or user.get("username") or "Кто-то"
    state["names"][uid] = name


def award_points(state, user_id, amount):
    if not user_id:
        return
    uid = str(user_id)
    state["scores"][uid] = state["scores"].get(uid, 0) + amount


def format_leaderboard(state, top_n=10):
    scores = state.get("scores", {})
    if not scores:
        return None
    names = state.get("names", {})
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, pts) in enumerate(ranked):
        marker = medals[i] if i < len(medals) else f"{i + 1}."
        name = names.get(uid, f"игрок {uid}")
        lines.append(f"{marker} {name} — {pts} очк.")
    return "\n".join(lines)


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

        if text_cmd == "/тест_победитель":
            riddle = state["active_riddle"]
            if not riddle:
                send_message(user_id, "[тест] Активной загадки нет")
            elif riddle.get("winner_id"):
                send_message(user_id, "[тест] Победитель уже назначен")
            else:
                riddle["winner_id"] = ADMIN_ID
                riddle["winner_name"] = "Тестовый победитель"
                send_message(user_id, "[тест] Победитель назначен ботом, можно проверять /тест_время")
            return True

        if text_cmd == "/тест_пинг":
            send_message(user_id, f"[тест] Бот жив, время сейчас: {now_msk().strftime('%H:%M:%S')}")
            return True

        if text_raw.lower().startswith("/broadcast"):
            parts = text_raw.split(maxsplit=1)
            if len(parts) < 2:
                send_message(user_id, "Напиши текст после команды: /broadcast текст")
            else:
                send_message(CHAT_ID, parts[1])
                send_message(user_id, "Отправлено в чат")
            return True

        if text_cmd == "/подсказка":
            riddle = state["active_riddle"]
            if not riddle:
                send_message(user_id, "Активной загадки нет, подсказывать нечего")
            else:
                first_letter = riddle["answer"].strip()[0].upper()
                send_message(CHAT_ID, f"💡 Подсказка: слово начинается на букву «{first_letter}»")
                send_message(user_id, "Подсказка отправлена в чат")
            return True

        if text_cmd == "/топ":
            text = format_leaderboard(state)
            send_message(user_id, text or "Пока ни у кого нет очков")
            return True

    # --- начать новую загадку ---
    if text_cmd == "/загадать":
        if state["active_riddle"]:
            send_message(user_id, "Сейчас уже есть активная загадка, дождись пока её отгадают")
        else:
            state["pending"][uid] = {"stage": "awaiting_photo"}
            send_message(user_id, "Приши мне фото крупным планом (можно сразу с подписью - названием предмета, тогда отвечать отдельным сообщением не понадобится)")
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

        if text_raw:
            # фото пришло сразу с подписью (названием) - публикуем сразу, без доп. шага
            state["active_riddle"] = {
                "author_id": user_id,
                "answer": text_raw.strip(),
                "photo_file_id": file_id,
                "created_date": today_str(),
                "winner_id": None,
                "winner_name": None,
            }
            state["pending"].pop(uid, None)
            send_photo(CHAT_ID, file_id, caption="Угадайте что это? 🔍")
            send_message(user_id, "Принято! Опубликовал в чат 🔍")
            return True

        # фото без подписи - как раньше, просим название отдельным сообщением
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

    # ✅ Молчим на случайные сообщения если пользователь не в процессе загадки
    if not pending and not text_cmd.startswith("/"):
        return False

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


def announce_winner(state, riddle):
    """Общая логика объявления победителя: сообщение в чат + начисление очков."""
    text = f"🎉 {riddle['winner_name']} угадал(а)! Ответ был: {riddle['answer']}"
    send_message(CHAT_ID, text)
    award_points(state, riddle["winner_id"], 1)
    award_points(state, riddle["author_id"], 1)
    state["active_riddle"] = None


def force_check_announcement(state):
    riddle = state["active_riddle"]
    if riddle and riddle.get("winner_id"):
        announce_winner(state, riddle)
        send_message(ADMIN_ID, "[тест] Объявление отправлено, очки начислены, загадка закрыта")
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
        announce_winner(state, riddle)

    state["last_announced_date"] = today


# ---------- еженедельный/ежемесячный топ ----------

def check_periodic_leaderboard(state):
    now = now_msk()

    # по понедельникам - топ недели
    if now.weekday() == 0:
        wk = week_key_str(now)
        if state.get("last_weekly_post") != wk:
            text = format_leaderboard(state)
            if text:
                send_message(CHAT_ID, "📅 Итоги недели:\n" + text)
            state["last_weekly_post"] = wk

    # 1 числа месяца - топ месяца
    if now.day == 1:
        mk = month_key_str(now)
        if state.get("last_monthly_post") != mk:
            text = format_leaderboard(state)
            if text:
                send_message(CHAT_ID, "🗓 Итоги месяца:\n" + text)
            state["last_monthly_post"] = mk


# ---------- еженедельный бэкап state ----------

def check_weekly_backup(state, fernet):
    now = now_msk()
    if now.weekday() != 0:  # только по понедельникам
        return
    wk = week_key_str(now)
    if state.get("last_backup_week") == wk:
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(BACKUP_DIR, f"state_{now.strftime('%Y-%m-%d')}.json")
    payload = json.dumps(state, ensure_ascii=False).encode()
    token = fernet.encrypt(payload).decode()
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump({"data": token}, f)

    state["last_backup_week"] = wk


# ---------- healthcheck ----------

def check_healthcheck(state):
    now = now_msk()
    today = today_str()

    if now.hour != 17:
        return
    if state.get("last_healthcheck_date") == today:
        return

    send_message(ADMIN_ID, f"✅ Бот жив, всё работает. {now.strftime('%d.%m.%Y %H:%M')} МСК")
    state["last_healthcheck_date"] = today


# ---------- основной цикл ----------

def main():
    fernet = get_fernet()
    state = load_state(fernet)

    # получаем ТОЛЬКО новые апдейты (после последнего обработанного)
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

        remember_name(state, user_id, user)

        if chat["type"] == "private":
            handle_private_message(state, user_id, user, text_raw, photo)
        elif chat["id"] == CHAT_ID:
            handle_group_message(state, user, text_raw)

    # ✅ ОБЯЗАТЕЛЬНО СОХРАНЯЙ НОВЫЙ max_update_id
    state["last_update_id"] = max_update_id

    check_daily_announcement(state)
    check_periodic_leaderboard(state)
    check_weekly_backup(state, fernet)
    check_healthcheck(state)

    save_state(fernet, state)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("[error] бот упал с ошибкой:")
        traceback.print_exc()
        raise

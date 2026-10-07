"""Обробка оновлень Telegram для webhook-режиму (Vercel).

Стан тестів і результати зберігаються в Upstash Redis (через REST API),
бо на Vercel код запускається лише на час обробки одного запиту.
Без змінних KV_REST_API_URL / KV_REST_API_TOKEN використовується пам'ять процесу
(годиться лише для локальної перевірки).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import quiz

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # немає бази часових поясів
    TZ = None

log = logging.getLogger("akredytatsia_bot")
BASE = Path(__file__).resolve().parent
BANK = quiz.load_bank(BASE / "questions.json")

SESSION_TTL = 7 * 24 * 3600      # незавершений тест зберігається тиждень
USER_TTL = 180 * 24 * 3600       # історія студента — пів року


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def admin_ids() -> set[int]:
    return {int(x) for x in env("ADMIN_IDS").replace(" ", "").split(",") if x.isdigit()}


def now_str(fmt: str = "%Y-%m-%d %H:%M") -> str:
    return (datetime.now(TZ) if TZ else datetime.now()).strftime(fmt)


# ---------- Сховище ----------

class Store:
    def __init__(self) -> None:
        self.url = (env("KV_REST_API_URL") or env("UPSTASH_REDIS_REST_URL")).rstrip("/")
        self.token = env("KV_REST_API_TOKEN") or env("UPSTASH_REDIS_REST_TOKEN")
        self.mem: dict = {}

    @property
    def persistent(self) -> bool:
        return bool(self.url and self.token)

    def _cmd(self, *args):
        if not self.persistent:
            return self._mem_cmd(*args)
        req = urllib.request.Request(self.url, data=json.dumps([str(a) for a in args]).encode(),
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.load(r)
        if "error" in body:
            raise RuntimeError(f"Redis: {body['error']}")
        return body.get("result")

    def _mem_cmd(self, cmd, key, *rest):
        cmd = cmd.upper()
        if cmd == "GET":
            return self.mem.get(key)
        if cmd == "SET":
            self.mem[key] = rest[0]
            return "OK"
        if cmd == "DEL":
            self.mem.pop(key, None)
            return 1
        if cmd == "RPUSH":
            self.mem.setdefault(key, []).append(rest[0])
            return len(self.mem[key])
        if cmd == "LRANGE":
            return list(self.mem.get(key, []))
        raise ValueError(cmd)

    def get_json(self, key: str):
        raw = self._cmd("GET", key)
        return json.loads(raw) if raw else None

    def set_json(self, key: str, value, ttl: int | None = None) -> None:
        args = ["SET", key, json.dumps(value, ensure_ascii=False)]
        if ttl and self.persistent:
            args += ["EX", ttl]
        self._cmd(*args)

    def delete(self, key: str) -> None:
        self._cmd("DEL", key)

    def push(self, key: str, value) -> None:
        self._cmd("RPUSH", key, json.dumps(value, ensure_ascii=False))

    def all(self, key: str) -> list:
        return [json.loads(x) for x in (self._cmd("LRANGE", key, 0, -1) or [])]


STORE = Store()


# ---------- Telegram Bot API ----------

class TgError(Exception):
    pass


def tg(method: str, **params):
    token = env("BOT_TOKEN")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(params, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            raise TgError(f"{method}: HTTP {e.code}") from e
    if not body.get("ok"):
        raise TgError(f"{method}: {body.get('description')}")
    return body.get("result")


def tg_document(chat_id: int, filename: str, content: bytes, caption: str = ""):
    token = env("BOT_TOKEN")
    boundary = uuid.uuid4().hex
    parts = []
    for name, val in (("chat_id", str(chat_id)), ("caption", caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                  f"Content-Type: text/csv\r\n\r\n").encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def send(chat_id: int, text: str, kb: dict | None = None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}}
    if kb:
        params["reply_markup"] = kb
    return tg("sendMessage", **params)


def edit(chat_id: int, message_id: int, text: str, kb: dict | None = None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}}
    if kb:
        params["reply_markup"] = kb
    try:
        tg("editMessageText", **params)
    except TgError as e:
        if "not modified" not in str(e).lower():
            raise


def drop_kb(chat_id: int, message_id: int) -> None:
    try:
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
           reply_markup={"inline_keyboard": []})
    except TgError:
        pass


def toast(callback_id: str, text: str | None = None, alert: bool = False) -> None:
    params = {"callback_query_id": callback_id, "show_alert": alert}
    if text:
        params["text"] = text
    try:
        tg("answerCallbackQuery", **params)
    except TgError:
        pass


# ---------- Клавіатури й тексти ----------

def B(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def kb(rows) -> dict:
    return {"inline_keyboard": rows}


def menu_kb() -> dict:
    n = len(BANK["questions"])
    return kb([
        [B(f"⚡ Швидкий тест ({quiz.QUICK_SIZE} питань)", "m|quick")],
        [B(f"🎯 Повний тест (усі {n} питань)", "m|full")],
        [B("📚 Тест за темою (блоком)", "m|blocks")],
        [B("ℹ️ Як це працює", "m|help")],
    ])


def blocks_kb() -> dict:
    rows = []
    for key, name in BANK["blocks"].items():
        cnt = sum(1 for q in BANK["questions"] if str(q["block"]) == key)
        rows.append([B(f"{key}. {name} ({cnt})", f"b|{key}")])
    rows.append([B("🏠 Меню", "m|menu")])
    return kb(rows)


def question_kb(s: dict) -> dict:
    return kb([
        [B(L, f"a|{s['id']}|{s['pos']}|{k}") for k, L in enumerate(quiz.LETTERS)],
        [B("⏹ Завершити тест", f"stop|{s['id']}")],
    ])


def after_answer_kb(s: dict) -> dict:
    last = s["pos"] + 1 >= len(s["items"])
    rows = [[B("📊 Показати результат" if last else "Наступне питання ➡️", f"n|{s['id']}")]]
    if not last:
        rows.append([B("⏹ Завершити тест", f"stop|{s['id']}")])
    return kb(rows)


def summary_kb(s: dict) -> dict:
    rows = [[B("🔁 Пройти ще раз (нове перемішування)", "m|retry")]]
    wrong = quiz.wrong_qis(s)
    if wrong:
        rows.append([B(f"🔄 Робота над помилками ({len(wrong)})", "m|mistakes")])
    rows.append([B("🏠 Меню", "m|menu")])
    return kb(rows)


HELP_TEXT = (
    "ℹ️ <b>Як працює тест</b>\n\n"
    "• Кожне питання має 4 варіанти відповіді — натисніть букву A, B, C або D.\n"
    "• Після кожної відповіді бот показує правильний варіант і <b>пояснення</b> — "
    "і коли ви відповіли правильно, і коли помилилися.\n"
    "• У кожній новій спробі <b>питання і варіанти перемішуються</b>, тож запам'ятати "
    "послідовність не вийде — тільки зрозуміти суть 🙂\n"
    "• Наприкінці — результат, теми для повторення і «Робота над помилками».\n\n"
    "<b>Режими:</b>\n"
    f"⚡ Швидкий — {quiz.QUICK_SIZE} випадкових питань з усіх тем;\n"
    "🎯 Повний — усі питання банку;\n"
    "📚 За темою — питання одного з 5 блоків.\n\n"
    "<b>Команди:</b> /start — меню, /stop — завершити тест, /mystats — мої результати."
)

WELCOME = (
    "👋 Вітаю! Це тренажер для підготовки до <b>акредитації освітньо-професійної програми</b>.\n\n"
    "Тут зібрано питання, які можуть поставити експерти під час акредитації, "
    "а також уточнюючі питання до них. Після кожної відповіді ви побачите пояснення.\n\n"
    "Оберіть режим 👇"
)

COMMANDS = [
    {"command": "start", "description": "Головне меню"},
    {"command": "help", "description": "Як працює тест"},
    {"command": "stop", "description": "Завершити тест"},
    {"command": "mystats", "description": "Мої результати"},
]


# ---------- Стан користувача ----------

def get_session(uid: int) -> dict | None:
    return STORE.get_json(f"s:{uid}")


def save_session(uid: int, s: dict) -> None:
    STORE.set_json(f"s:{uid}", s, SESSION_TTL)


def get_user(uid: int) -> dict:
    return STORE.get_json(f"u:{uid}") or {}


def save_user(uid: int, u: dict) -> None:
    STORE.set_json(f"u:{uid}", u, USER_TTL)


def start_session(chat_id: int, uid: int, mode: str, block: str | None = None,
                  only: list[int] | None = None) -> None:
    s = quiz.new_session(BANK, mode, block=block, only=only)
    if not s["items"]:
        send(chat_id, "Немає питань для цього режиму.", menu_kb())
        return
    save_session(uid, s)
    send(chat_id, f"▶️ <b>{s['title']}</b> — {len(s['items'])} питань. Порядок питань і варіантів перемішано.")
    send(chat_id, quiz.render_question(BANK, s), question_kb(s))


def finish(chat_id: int, user: dict, s: dict) -> None:
    uid = user["id"]
    u = get_user(uid)
    u["last"] = {"mode": s["mode"], "block": s["block"], "wrong": quiz.wrong_qis(s)}
    done = len(quiz.answered_items(s))
    if done:
        hist = u.setdefault("history", [])
        hist.append({"date": now_str("%d.%m %H:%M"), "title": s["title"], "score": s["score"], "total": done})
        del hist[:-10]
        full_name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
        try:
            STORE.push("results", [now_str(), uid, user.get("username") or "", full_name, s["title"],
                                   s["score"], done, len(s["items"]), round(100 * s["score"] / done)])
        except Exception:
            log.exception("Не вдалося записати результат")
    save_user(uid, u)
    STORE.delete(f"s:{uid}")
    send(chat_id, quiz.render_summary(BANK, s), summary_kb(s))


# ---------- Обробники ----------

def on_command(msg: dict) -> None:
    chat_id, user = msg["chat"]["id"], msg["from"]
    uid = user["id"]
    cmd = msg["text"].split()[0].split("@")[0].lower()
    if cmd in ("/start", "/menu"):
        STORE.delete(f"s:{uid}")
        send(chat_id, WELCOME, menu_kb())
    elif cmd == "/help":
        send(chat_id, HELP_TEXT, menu_kb())
    elif cmd == "/stop":
        s = get_session(uid)
        if s:
            finish(chat_id, user, s)
        else:
            send(chat_id, "Зараз немає активного тесту.", menu_kb())
    elif cmd == "/mystats":
        hist = get_user(uid).get("history", [])
        if not hist:
            send(chat_id, "Ви ще не завершили жодного тесту.", menu_kb())
            return
        lines = ["📈 <b>Ваші останні результати</b>", ""]
        for h in reversed(hist):
            lines.append(f"{h['date']} — {h['title']}: {h['score']}/{h['total']} "
                         f"({round(100 * h['score'] / h['total'])}%)")
        send(chat_id, "\n".join(lines), menu_kb())
    elif cmd == "/myid":
        send(chat_id, f"Ваш Telegram ID: <code>{uid}</code>")
    elif cmd == "/report":
        if uid not in admin_ids():
            send(chat_id, "Ця команда доступна лише викладачам (адміністраторам бота).")
            return
        rows = STORE.all("results")
        if not rows:
            send(chat_id, "Результатів поки немає.")
            return
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Дата і час", "Telegram ID", "Username", "Ім'я", "Режим",
                    "Правильних", "Відповідей", "Питань у тесті", "Відсоток"])
        w.writerows(rows)
        tg_document(chat_id, "rezultaty_testuvannia.csv", buf.getvalue().encode("utf-8-sig"),
                    f"Результати тестування: {len(rows)} записів (відкривається в Excel).")
    else:
        send(chat_id, "Не знаю такої команди. Натисніть /start, щоб відкрити меню.")


def on_callback(cq: dict) -> None:
    cid = cq["id"]
    user = cq["from"]
    uid = user["id"]
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id", uid)
    mid = msg.get("message_id")
    parts = (cq.get("data") or "").split("|")

    if parts[0] == "m":
        toast(cid)
        action = parts[1] if len(parts) > 1 else "menu"
        u = get_user(uid)
        if action == "menu":
            send(chat_id, "Оберіть режим 👇", menu_kb())
        elif action == "help":
            send(chat_id, HELP_TEXT, menu_kb())
        elif action == "blocks":
            send(chat_id, "Оберіть тему 👇", blocks_kb())
        elif action in ("quick", "full"):
            start_session(chat_id, uid, action)
        elif action == "retry":
            last = u.get("last") or {"mode": "quick", "block": None}
            if last["mode"] == "mistakes":
                start_session(chat_id, uid, "mistakes", only=u.get("mistakes_pool", []))
            else:
                start_session(chat_id, uid, last["mode"], block=last.get("block"))
        elif action == "mistakes":
            wrong = (u.get("last") or {}).get("wrong", [])
            if not wrong:
                send(chat_id, "Помилок немає — чудово! 🎉", menu_kb())
            else:
                u["mistakes_pool"] = list(wrong)
                save_user(uid, u)
                start_session(chat_id, uid, "mistakes", only=wrong)
        return

    if parts[0] == "b" and len(parts) > 1 and parts[1] in BANK["blocks"]:
        toast(cid)
        start_session(chat_id, uid, "block", block=parts[1])
        return

    s = get_session(uid)
    if not s or s.get("finished") or len(parts) < 2 or parts[1] != s["id"]:
        toast(cid, "Цей тест уже завершено. Почніть новий через /start.", alert=True)
        return

    if parts[0] == "a" and len(parts) == 4:
        pos, choice = int(parts[2]), int(parts[3])
        item = quiz.current(s)
        if pos != s["pos"] or item is None or item["chosen"] is not None or not 0 <= choice < 4:
            toast(cid, "На це питання ви вже відповіли.")
            return
        ok = quiz.answer(s, choice)
        save_session(uid, s)
        toast(cid, "✅ Правильно!" if ok else "❌ Неправильно")
        edit(chat_id, mid, quiz.render_feedback(BANK, s), after_answer_kb(s))
        return

    if parts[0] == "n":
        item = quiz.current(s)
        if item is None or item["chosen"] is None:
            toast(cid, "Спочатку оберіть відповідь.")
            return
        toast(cid)
        drop_kb(chat_id, mid)
        quiz.advance(s)
        if s["finished"]:
            finish(chat_id, user, s)
        else:
            save_session(uid, s)
            send(chat_id, quiz.render_question(BANK, s), question_kb(s))
        return

    if parts[0] == "stop":
        toast(cid, "Тест завершено")
        drop_kb(chat_id, mid)
        finish(chat_id, user, s)
        return

    toast(cid)


def handle_update(update: dict) -> None:
    if "callback_query" in update:
        on_callback(update["callback_query"])
        return
    msg = update.get("message")
    if not msg or msg.get("chat", {}).get("type") != "private":
        return
    text = msg.get("text") or ""
    if text.startswith("/"):
        on_command(msg)
    else:
        send(msg["chat"]["id"], "Натисніть /start, щоб відкрити меню тестування 👇", menu_kb())

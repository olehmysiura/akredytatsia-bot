"""Точка входу для Vercel (Flask).

Маршрути:
  GET  /                     — перевірка, що бот розгорнуто
  POST /webhook              — сюди Telegram надсилає повідомлення
  GET  /setup?key=СЕКРЕТ     — одноразово підключає webhook (відкрити в браузері після деплою)
"""
from __future__ import annotations

import hmac
import logging
import os

from flask import Flask, jsonify, request

import core

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)


def _secret_ok(given: str | None) -> bool:
    secret = core.env("WEBHOOK_SECRET")
    return bool(secret) and hmac.compare_digest((given or "").encode(), secret.encode())


@app.get("/")
def index():
    return jsonify({
        "status": "ok",
        "questions": len(core.BANK["questions"]),
        "token_set": bool(core.env("BOT_TOKEN")),
        "secret_set": bool(core.env("WEBHOOK_SECRET")),
        "database_connected": core.STORE.persistent,
        # Діагностика: лише назви змінних і тип середовища, без значень
        "vercel_env": os.environ.get("VERCEL_ENV"),
        "commit": (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "")[:7],
        "env_names": sorted(k for k in os.environ if k.startswith(
            ("BOT_", "WEBHOOK", "ADMIN", "KV_", "REDIS", "UPSTASH"))),
    })


@app.post("/webhook")
def webhook():
    if not _secret_ok(request.headers.get("X-Telegram-Bot-Api-Secret-Token")):
        return "forbidden", 403
    update = request.get_json(silent=True) or {}
    try:
        core.handle_update(update)
    except Exception:
        # Відповідаємо 200, щоб Telegram не надсилав те саме оновлення повторно
        logging.exception("Помилка обробки оновлення")
    return "ok"


@app.get("/setup")
def setup():
    if not _secret_ok(request.args.get("key")):
        return "Невірний або не заданий ключ. Відкрийте /setup?key=ВАШ_WEBHOOK_SECRET", 403
    missing = [n for n in ("BOT_TOKEN", "WEBHOOK_SECRET") if not core.env(n)]
    if missing:
        return f"Не задано змінні середовища: {', '.join(missing)}", 500
    url = f"https://{request.host}/webhook"
    try:
        core.tg("setWebhook", url=url, secret_token=core.env("WEBHOOK_SECRET"),
                allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        core.tg("setMyCommands", commands=core.COMMANDS)
        me = core.tg("getMe")
    except core.TgError as e:
        return f"Помилка Telegram: {e}. Перевірте BOT_TOKEN.", 500
    db = "підключено" if core.STORE.persistent else "НЕ ПІДКЛЮЧЕНО — додайте Upstash Redis (див. README)"
    return (f"✅ Готово! Webhook для @{me.get('username')} встановлено на {url}\n"
            f"База даних: {db}\n"
            f"Відкрийте бота в Telegram: https://t.me/{me.get('username')}"), 200, {
        "Content-Type": "text/plain; charset=utf-8"}

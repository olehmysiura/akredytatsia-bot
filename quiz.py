"""Логіка тестування: банк питань, сесії, перемішування, тексти повідомлень.

Модуль не залежить від Telegram — його можна перевіряти окремо.
"""
from __future__ import annotations

import html
import json
import random
import secrets
from pathlib import Path

LETTERS = ["A", "B", "C", "D"]
QUICK_SIZE = 15


def load_bank(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        bank = json.load(f)
    for i, q in enumerate(bank["questions"]):
        if len(q["wrong"]) != 3 or q["correct"] in q["wrong"]:
            raise ValueError(f"Питання #{i} має некоректні варіанти: {q['q']}")
        if str(q["block"]) not in bank["blocks"]:
            raise ValueError(f"Питання #{i} має невідомий блок {q['block']}")
    return bank


def _make_item(bank: dict, qi: int, rng: random.Random) -> dict:
    q = bank["questions"][qi]
    opts = [q["correct"]] + list(q["wrong"])
    rng.shuffle(opts)  # нове перемішування варіантів у кожній сесії
    return {"qi": qi, "opts": opts, "correct": opts.index(q["correct"]), "chosen": None}


def new_session(bank: dict, mode: str, block: str | None = None,
                only: list[int] | None = None, rng: random.Random | None = None) -> dict:
    """Створює сесію. mode: full | quick | block | mistakes."""
    rng = rng or random.SystemRandom()
    n = len(bank["questions"])
    if mode == "full":
        pool, title = list(range(n)), "Повний тест"
    elif mode == "quick":
        pool, title = list(range(n)), "Швидкий тест"
    elif mode == "block":
        pool = [i for i, q in enumerate(bank["questions"]) if str(q["block"]) == str(block)]
        title = f"Блок {block}. {bank['blocks'][str(block)]}"
    elif mode == "mistakes":
        pool, title = [i for i in (only or []) if 0 <= i < n], "Робота над помилками"
    else:
        raise ValueError(mode)
    rng.shuffle(pool)  # нове перемішування порядку питань у кожній сесії
    if mode == "quick":
        pool = pool[:QUICK_SIZE]
    return {
        "id": secrets.token_hex(4),
        "mode": mode,
        "block": block,
        "title": title,
        "items": [_make_item(bank, qi, rng) for qi in pool],
        "pos": 0,
        "score": 0,
        "finished": False,
    }


def current(session: dict) -> dict | None:
    if session["pos"] < len(session["items"]):
        return session["items"][session["pos"]]
    return None


def answer(session: dict, choice: int) -> bool:
    """Фіксує відповідь на поточне питання. Повертає True, якщо правильно."""
    item = current(session)
    if item is None or item["chosen"] is not None:
        raise ValueError("Немає активного питання")
    item["chosen"] = choice
    ok = choice == item["correct"]
    if ok:
        session["score"] += 1
    return ok


def advance(session: dict) -> None:
    session["pos"] += 1
    if session["pos"] >= len(session["items"]):
        session["finished"] = True


def answered_items(session: dict) -> list[dict]:
    return [it for it in session["items"] if it["chosen"] is not None]


def wrong_qis(session: dict) -> list[int]:
    return [it["qi"] for it in answered_items(session) if it["chosen"] != it["correct"]]


# ---------- Тексти повідомлень (HTML) ----------

def _e(s: str) -> str:
    return html.escape(s, quote=False)


def _header(bank: dict, session: dict, item: dict) -> str:
    q = bank["questions"][item["qi"]]
    block = bank["blocks"][str(q["block"])]
    return (f"📝 <b>Питання {session['pos'] + 1} з {len(session['items'])}</b>\n"
            f"<i>{_e(block)} · {_e(q['ref'])}</i>\n\n"
            f"<b>{_e(q['q'])}</b>")


def render_question(bank: dict, session: dict) -> str:
    item = current(session)
    lines = [_header(bank, session, item), ""]
    for k, opt in enumerate(item["opts"]):
        lines.append(f"<b>{LETTERS[k]})</b> {_e(opt)}")
        lines.append("")
    lines.append("Оберіть варіант відповіді 👇")
    return "\n".join(lines)


def render_feedback(bank: dict, session: dict) -> str:
    """Текст після відповіді: позначки, правильна відповідь і пояснення (завжди)."""
    item = current(session)
    q = bank["questions"][item["qi"]]
    lines = [_header(bank, session, item), ""]
    for k, opt in enumerate(item["opts"]):
        mark = "✅" if k == item["correct"] else ("❌" if k == item["chosen"] else "▫️")
        lines.append(f"{mark} <b>{LETTERS[k]})</b> {_e(opt)}")
    lines.append("")
    if item["chosen"] == item["correct"]:
        lines.append("✅ <b>Правильно!</b>")
    else:
        lines.append(f"❌ <b>Неправильно.</b> Ви обрали {LETTERS[item['chosen']]}.")
        lines.append(f"Правильна відповідь — <b>{LETTERS[item['correct']]})</b> {_e(item['opts'][item['correct']])}")
    lines.append("")
    lines.append(f"💡 <b>Пояснення.</b> {_e(q['explain'])}")
    lines.append("")
    lines.append(f"Рахунок: {session['score']} з {len(answered_items(session))}")
    return "\n".join(lines)


def verdict(percent: float) -> str:
    if percent >= 90:
        return "🏆 Відмінно! Ви готові до зустрічі з експертами."
    if percent >= 75:
        return "👍 Добре! Повторіть теми, де були помилки."
    if percent >= 50:
        return "📘 Задовільно. Варто ще раз переглянути презентацію і пройти тест знову."
    return "📚 Матеріал потрібно повторити. Перегляньте презентацію і спробуйте ще раз."


def render_summary(bank: dict, session: dict) -> str:
    done = answered_items(session)
    total = len(done)
    score = session["score"]
    percent = round(100 * score / total) if total else 0
    lines = [f"🏁 <b>{_e(session['title'])} — завершено</b>", ""]
    if not total:
        lines.append("Ви не відповіли на жодне питання.")
        return "\n".join(lines)
    if total < len(session["items"]):
        lines.append(f"Пройдено {total} з {len(session['items'])} питань.")
    lines.append(f"Результат: <b>{score} з {total}</b> ({percent}%)")
    lines.append(verdict(percent))
    wrong = wrong_qis(session)
    if wrong:
        refs = []
        for qi in wrong:
            r = bank["questions"][qi]["ref"]
            if r not in refs:
                refs.append(r)
        lines.append("")
        lines.append("🔎 <b>Теми для повторення:</b> " + _e(", ".join(refs)))
        lines.append("Натисніть «Робота над помилками», щоб пройти ці питання ще раз (у новому порядку).")
    return "\n".join(lines)

"""
Telegram-бот — голосовой ассистент для дневных заметок.

Как работает:
  1. Надиктовываешь голосовое (или пишешь текст).
  2. Whisper (OpenAI) расшифровывает аудио в текст.
  3. Claude приводит расшифровку в порядок: чистит ошибки распознавания,
     структурирует по пунктам, выделяет цифры/суммы и даёт короткую выжимку.
  4. Заметка сохраняется в файл текущего дня.
  5. Команда /day собирает сводную выжимку за весь день — её кладёшь в Cowork
     «daily dollar balance».

Позже функционал легко расширить (категории, экспорт, автосводка вечером).
Все ключи — через переменные окружения (см. .env.example и README).
"""

from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone

import requests
from anthropic import Anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- Настройки из переменных окружения ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]           # для расшифровки голосовых (Whisper)
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
NOTES_DIR = os.environ.get("NOTES_DIR", "notes")

# Доступ: список разрешённых Telegram user id (через запятую). Пусто => не пускаем никого.
ALLOWED_USER_IDS = {
    uid.strip()
    for uid in os.environ.get("ALLOWED_USER_ID", "").replace(";", ",").split(",")
    if uid.strip()
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice-notes-bot")
anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------- Промпты ----------
STRUCTURE_PROMPT = """Ты приводишь в порядок надиктованную голосовую заметку.
На вход — сырая расшифровка речи на русском (возможны ошибки распознавания).

Сделай:
1. Аккуратно исправь очевидные ошибки распознавания и пунктуацию, НЕ выдумывая
   фактов, которых не было. Числа, суммы, имена сохраняй точно.
2. Структурируй содержание по пунктам (маркированный список), сгруппировав по смыслу.
3. Отдельно выдели все цифры/суммы/деньги, если они есть.

Верни ответ в таком виде (без лишних пояснений):

📝 <краткая выжимка одной-двумя строками>

• <пункт>
• <пункт>
..."""

DIGEST_PROMPT = """Тебе дают несколько структурированных заметок за один день.
Собери из них ОДНУ сводную выжимку дня — так, чтобы её можно было целиком
вставить в рабочий документ.

Требования:
- Объедини повторяющееся, убери воду, сохрани все конкретные цифры, суммы и имена.
- Сгруппируй по темам, используй маркированные пункты.
- В самом верху — 2-3 строки итога дня.
- Пиши по-русски, деловым и компактным стилем."""

ROUTER_PROMPT = """Пользователь ведёт голосовые заметки. Определи намерение сообщения.
Верни СТРОГО JSON: {"action": "...", "instruction": "..."}.

action:
- "delete_last" — просит удалить/убрать последнюю запись (возможно, чтобы продиктовать заново).
- "edit_last"   — просит исправить/переделать последнюю запись. instruction = суть правки
  (что именно изменить). Если конкретной правки в сообщении нет — instruction оставь пустым.
- "note"        — обычная новая заметка, а не команда правки. Значение по умолчанию.

Правила:
- Командой считай только явные инструкции о ПОСЛЕДНЕЙ/ПРЕДЫДУЩЕЙ записи, например
  "удали последнюю запись", "переделай последнюю", "исправь в последней записи сумму на 500".
- Если сообщение просто описывает события/факты — это "note", даже если в нём встречаются
  слова «удалил», «последний», «исправил» и т.п.
- Сомневаешься — "note"."""


# ---------- Транскрипция ----------
def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": f},
            data={"model": "whisper-1", "language": "ru"},
            timeout=120,
        )
    r.raise_for_status()
    return r.json().get("text", "").strip()


# ---------- Модель ----------
def _claude(system: str, user: str, max_tokens: int = 2048) -> str:
    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _extract_json(raw: str) -> dict:
    """Достаёт JSON-объект из ответа модели, даже если он обёрнут в текст/```json."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"в ответе модели нет JSON: {raw[:200]}")
    return json.loads(raw[start : end + 1])


def structure_note(text: str) -> str:
    return _claude(STRUCTURE_PROMPT, text)


def restructure_with_correction(transcript: str, structured: str, instruction: str) -> str:
    """Пересобирает последнюю заметку с учётом правки пользователя."""
    user = (
        f"Текущая структурированная заметка:\n{structured}\n\n"
        f"Исходная расшифровка:\n{transcript}\n\n"
        f"Правка пользователя: {instruction}\n\n"
        "Применни правку и верни обновлённую заметку в том же формате."
    )
    return _claude(STRUCTURE_PROMPT, user)


def looks_like_edit(text: str) -> bool:
    """Дешёвый пред-фильтр: похоже ли сообщение на команду правки записи.
    Если да — стоит спросить у модели-роутера; иначе это точно обычная заметка."""
    t = text.lower()
    has_target = any(w in t for w in ("последн", "предыдущ", "запись", "заметк"))
    has_verb = any(
        w in t
        for w in ("удал", "убер", "сотри", "переделай", "пересоздай",
                  "исправь", "поправь", "замени", "заново", "измени")
    )
    return has_target and has_verb


def route_message(text: str) -> dict:
    """Классифицирует сообщение: обычная заметка или команда правки. Консервативен."""
    if not looks_like_edit(text):
        return {"action": "note"}
    try:
        data = _extract_json(_claude(ROUTER_PROMPT, text, max_tokens=300))
        if data.get("action") in ("delete_last", "edit_last", "note"):
            return data
    except Exception:
        log.exception("router error")
    return {"action": "note"}


def make_digest(notes: list) -> str:
    joined = "\n\n---\n\n".join(
        f"[{n['ts']}]\n{n['structured']}" for n in notes
    )
    return _claude(DIGEST_PROMPT, joined, max_tokens=3000)


# ---------- Хранение заметок по дням ----------
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_path(user_id: str, day: str) -> str:
    d = os.path.join(NOTES_DIR, str(user_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{day}.json")


def load_day(user_id: str, day: str) -> list:
    path = _day_path(user_id, day)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_day(user_id: str, day: str, notes: list) -> None:
    with open(_day_path(user_id, day), "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)


def append_note(user_id: str, transcript: str, structured: str) -> None:
    day = _today()
    notes = load_day(user_id, day)
    notes.append(
        {
            "ts": datetime.now(timezone.utc).strftime("%H:%M"),
            "transcript": transcript,
            "structured": structured,
        }
    )
    save_day(user_id, day, notes)


def delete_last(user_id: str) -> dict | None:
    """Удаляет последнюю запись за сегодня, возвращает её (или None, если пусто)."""
    day = _today()
    notes = load_day(user_id, day)
    if not notes:
        return None
    removed = notes.pop()
    save_day(user_id, day, notes)
    return removed


def replace_last_structured(user_id: str, new_structured: str, instruction: str) -> dict | None:
    """Заменяет структуру последней записи (после правки), возвращает её (или None)."""
    day = _today()
    notes = load_day(user_id, day)
    if not notes:
        return None
    notes[-1]["structured"] = new_structured
    notes[-1]["transcript"] += f"\n[правка] {instruction}"
    save_day(user_id, day, notes)
    return notes[-1]


# ---------- Доступ ----------
def allowed(update: Update) -> bool:
    # Fail-closed: если список пуст — не пускаем НИКОГО.
    if not ALLOWED_USER_IDS:
        return False
    user = update.effective_user
    return bool(user) and str(user.id) in ALLOWED_USER_IDS


# ---------- Хендлеры ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Надиктовывай голосовые — я расшифрую, структурирую и дам выжимку.\n\n"
        "• голосовое или текст — заметка на сегодня\n"
        "• «удали последнюю запись» / «исправь в последней записи …» — правка последней\n"
        "• /list — показать записи за сегодня\n"
        "• /undo — удалить последнюю запись\n"
        "• /day — сводная выжимка за день (её кладёшь в Cowork)\n"
        "• /clear — очистить заметки за сегодня\n\n"
        f"Твой Telegram user id: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    notes = load_day(str(update.effective_user.id), _today())
    if not notes:
        await update.message.reply_text("За сегодня пока нет заметок.")
        return
    lines = [f"Записи за {_today()} ({len(notes)}):"]
    for i, n in enumerate(notes, 1):
        summary = (n.get("structured") or "").splitlines()[0] if n.get("structured") else ""
        lines.append(f"{i}. [{n.get('ts', '')}] {summary}")
    await update.message.reply_text("\n".join(lines))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    removed = delete_last(str(update.effective_user.id))
    if not removed:
        await update.message.reply_text("Нечего удалять — за сегодня записей нет.")
        return
    await update.message.reply_text(
        "🗑 Удалил последнюю запись:\n\n"
        + removed["structured"]
        + "\n\nМожешь продиктовать заново."
    )


async def day_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    user_id = str(update.effective_user.id)
    notes = load_day(user_id, _today())
    if not notes:
        await update.message.reply_text("За сегодня пока нет заметок.")
        return
    await update.message.chat.send_action("typing")
    try:
        digest = make_digest(notes)
    except Exception as e:
        log.exception("digest error")
        await update.message.reply_text(f"Не смог собрать сводку: {e}")
        return
    header = f"🗓 <b>Daily dollar balance — {_today()}</b> ({len(notes)} заметок)\n\n"
    await update.message.reply_text(header + digest, parse_mode="HTML")


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    path = _day_path(str(update.effective_user.id), _today())
    if os.path.exists(path):
        os.remove(path)
    await update.message.reply_text("Заметки за сегодня очищены.")


async def handle_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        log.warning("Отказ в доступе: user_id=%s", update.effective_user.id)
        await update.message.reply_text("Доступ только для владельца бота.")
        return

    user_id = str(update.effective_user.id)
    await update.message.chat.send_action("typing")

    # Получаем текст: из голосового (Whisper) или напрямую.
    if update.message.voice or update.message.audio:
        media = update.message.voice or update.message.audio
        tg_file = await media.get_file()
        path = f"/tmp/{media.file_id}.ogg"
        await tg_file.download_to_drive(path)
        try:
            transcript = transcribe_voice(path)
        except Exception as e:
            log.exception("transcribe error")
            await update.message.reply_text(f"Не смог расшифровать голосовое: {e}")
            return
        finally:
            if os.path.exists(path):
                os.remove(path)
        if not transcript:
            await update.message.reply_text("Пустая расшифровка — попробуй ещё раз.")
            return
    else:
        transcript = update.message.text or ""
        if not transcript.strip():
            return

    # Команда правки или обычная заметка?
    route = route_message(transcript)
    action = route.get("action", "note")

    if action == "delete_last":
        removed = delete_last(user_id)
        if not removed:
            await update.message.reply_text("Нечего удалять — за сегодня записей нет.")
        else:
            await update.message.reply_text(
                "🗑 Удалил последнюю запись:\n\n"
                + removed["structured"]
                + "\n\nМожешь продиктовать заново."
            )
        return

    if action == "edit_last":
        notes = load_day(user_id, _today())
        if not notes:
            await update.message.reply_text("Нет записей за сегодня, чтобы править.")
            return
        instruction = (route.get("instruction") or "").strip()
        if not instruction:
            await update.message.reply_text(
                "Что именно поправить в последней записи? Напиши правку "
                "(например «сумма была 500, а не 5000»), либо скажи «удали последнюю» "
                "и продиктуй заново."
            )
            return
        last = notes[-1]
        try:
            new_structured = restructure_with_correction(
                last["transcript"], last["structured"], instruction
            )
        except Exception as e:
            log.exception("edit error")
            await update.message.reply_text(f"Не смог применить правку: {e}")
            return
        replace_last_structured(user_id, new_structured, instruction)
        await update.message.reply_text("✏️ Обновил последнюю запись:\n\n" + new_structured)
        return

    # Обычная заметка — структурируем и сохраняем.
    try:
        structured = structure_note(transcript)
    except Exception as e:
        log.exception("structure error")
        await update.message.reply_text(f"Не смог обработать заметку: {e}")
        return

    append_note(user_id, transcript, structured)
    await update.message.reply_text(structured)


def main():
    if not ALLOWED_USER_IDS:
        log.warning(
            "ALLOWED_USER_ID не задан — бот НИКОГО не пустит. "
            "Узнай свой id через /start и добавь его в переменные окружения."
        )
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("day", day_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_note))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_note))
    log.info("Бот запущен (модель: %s).", MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()

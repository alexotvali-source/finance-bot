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
from telegram import Update, ReplyKeyboardMarkup, BotCommand
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

DIGEST_PROMPT = """Тебе дают заметки за один день. Собери КОМПАКТНУЮ сводку —
её вставят в рабочий документ как есть.

Строго:
- Только факты из заметок: суммы, балансы, движения денег, имена. Ничего не добавляй
  и не придумывай.
- НЕ дублируй один и тот же факт в разных формах — каждый факт ровно один раз.
- НЕ выдумывай заголовки и разделы (никаких «Финансовое состояние», «Выжимка дня»
  и т.п.). Заголовок уместен, только если тем реально несколько.
- Объём пропорционален содержанию: одна заметка — одна-две строки.
- Формат — простой маркированный список, без вводных фраз.
- Пиши по-русски, кратко и по делу."""

ROUTER_PROMPT = """Пользователь ведёт нумерованные голосовые заметки за день.
Тебе дают СПИСОК записей (номер + краткая выжимка) и новое СООБЩЕНИЕ.
Определи намерение. Верни СТРОГО JSON: {"action": "...", "target": <номер|null>, "instruction": "..."}.

action:
- "delete" — удалить конкретную запись. target = её номер из списка.
- "edit"   — исправить/переделать конкретную запись. target = номер, instruction = суть правки.
- "note"   — обычная новая заметка (значение по умолчанию).

Как определить target (номер записи из списка):
- "последнюю" = последний номер; "предпоследнюю" = предпоследний.
- "вторую запись" / "запись 2" = 2; "первую" = 1; и т.д.
- можно ссылаться по смыслу: "исправь запись про аренду" → номер записи, где речь про аренду.
- если это команда правки/удаления, но какую именно запись — непонятно, target = null.

Правила:
- Командой считай только явные инструкции править/удалять запись(и). Простое описание
  событий/фактов — это "note" (target null), даже если есть слова «удалил», «исправил».
- Для "edit" без конкретной правки instruction оставь пустым.
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
    has_target = any(
        w in t
        for w in ("последн", "предпоследн", "предыдущ", "запис", "заметк",
                  "перв", "втор", "трет", "четверт", "пят", "шест", "седьм",
                  "восьм", "девят", "десят")
    ) or any(ch.isdigit() for ch in t)
    has_verb = any(
        w in t
        for w in ("удал", "убер", "сотри", "переделай", "пересоздай",
                  "исправь", "поправь", "замени", "заново", "измени")
    )
    return has_target and has_verb


def route_message(text: str, notes: list) -> dict:
    """Классифицирует сообщение: обычная заметка или команда правки/удаления записи.
    Роутер видит нумерованный список записей, чтобы определить номер (target)."""
    if not looks_like_edit(text):
        return {"action": "note"}
    listing = format_list(notes) or "(записей нет)"
    user = f"СПИСОК ЗАПИСЕЙ:\n{listing}\n\nСООБЩЕНИЕ:\n{text}"
    try:
        data = _extract_json(_claude(ROUTER_PROMPT, user, max_tokens=300))
        if data.get("action") in ("delete", "edit", "note"):
            return data
    except Exception:
        log.exception("router error")
    return {"action": "note"}


def make_digest(notes: list, label_dates: bool = False) -> str:
    parts = []
    for n in notes:
        prefix = f"[{n.get('date', '')} {n.get('ts', '')}]".strip() if label_dates else f"[{n.get('ts', '')}]"
        parts.append(f"{prefix}\n{n['structured']}")
    return _claude(DIGEST_PROMPT, "\n\n---\n\n".join(parts), max_tokens=3000)


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


def list_days(user_id: str) -> list:
    """Все даты (YYYY-MM-DD), за которые есть записи, по возрастанию."""
    d = os.path.join(NOTES_DIR, str(user_id))
    if not os.path.isdir(d):
        return []
    days = [f[:-5] for f in os.listdir(d) if f.endswith(".json") and _valid_date(f[:-5])]
    return sorted(days)


def load_range(user_id: str, days_back: int) -> list:
    """Записи за последние days_back дней (каждой добавлено поле 'date')."""
    today = datetime.now(timezone.utc).date()
    result = []
    for d in list_days(user_id):
        dd = datetime.strptime(d, "%Y-%m-%d").date()
        if 0 <= (today - dd).days < days_back:
            for n in load_day(user_id, d):
                n = dict(n)
                n["date"] = d
                result.append(n)
    return result


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


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


def _resolve_index(notes: list, target) -> int | None:
    """Переводит 1-based номер (может прийти строкой) в 0-based индекс или None."""
    try:
        i = int(target)
    except (TypeError, ValueError):
        return None
    return i - 1 if 1 <= i <= len(notes) else None


def delete_at(user_id: str, target) -> dict | None:
    """Удаляет запись по 1-based номеру за сегодня. Возвращает удалённую (или None)."""
    day = _today()
    notes = load_day(user_id, day)
    idx = _resolve_index(notes, target)
    if idx is None:
        return None
    removed = notes.pop(idx)
    save_day(user_id, day, notes)
    return removed


def delete_last(user_id: str) -> dict | None:
    """Удобная обёртка для /undo — удаляет последнюю запись за сегодня."""
    notes = load_day(user_id, _today())
    return delete_at(user_id, len(notes)) if notes else None


def replace_at(user_id: str, target, new_structured: str, instruction: str) -> dict | None:
    """Заменяет структуру записи по 1-based номеру. Возвращает её (или None)."""
    day = _today()
    notes = load_day(user_id, day)
    idx = _resolve_index(notes, target)
    if idx is None:
        return None
    notes[idx]["structured"] = new_structured
    notes[idx]["transcript"] += f"\n[правка] {instruction}"
    save_day(user_id, day, notes)
    return notes[idx]


def _first_line(note: dict) -> str:
    s = note.get("structured") or ""
    return s.splitlines()[0] if s else ""


def format_list(notes: list) -> str:
    """Нумерованный список записей: '1. [12:30] 📝 выжимка'."""
    return "\n".join(
        f"{i}. [{n.get('ts', '')}] {_first_line(n)}" for i, n in enumerate(notes, 1)
    )


# ---------- Кнопки ----------
BTN_LIST = "📋 Список"
BTN_DAY = "🗓 Сводка дня"
BTN_UNDO = "↩️ Удалить последнюю"
BTN_CLEAR = "🧹 Очистить день"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_LIST, BTN_DAY], [BTN_UNDO, BTN_CLEAR]],
    resize_keyboard=True,
)


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
        "• правка любой записи по номеру из /list: «удали вторую запись», "
        "«исправь запись 3: сумма 500», «переделай предпоследнюю»\n"
        "• /list — показать записи за сегодня (с номерами)\n"
        "• /undo — удалить последнюю запись\n"
        "• /day — сводка за день (или /day ГГГГ-ММ-ДД за прошлый)\n"
        "• /week — сводка за 7 дней\n"
        "• /history — дни, за которые есть записи\n"
        "• /find текст — поиск по всем записям\n"
        "• /clear — очистить заметки за сегодня\n\n"
        "Кнопки внизу — быстрый доступ к основным действиям.\n\n"
        f"Твой Telegram user id: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатия кнопок клавиатуры — вызывают соответствующие команды."""
    text = update.message.text
    if text == BTN_LIST:
        return await list_cmd(update, context)
    if text == BTN_DAY:
        return await day_cmd(update, context)
    if text == BTN_UNDO:
        return await undo_cmd(update, context)
    if text == BTN_CLEAR:
        return await clear_cmd(update, context)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    notes = load_day(str(update.effective_user.id), _today())
    if not notes:
        await update.message.reply_text("За сегодня пока нет заметок.")
        return
    await update.message.reply_text(
        f"Записи за {_today()} ({len(notes)}):\n" + format_list(notes)
    )


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
    args = context.args or []
    day = _today()
    if args:
        if _valid_date(args[0]):
            day = args[0]
        else:
            await update.message.reply_text("Дата в формате ГГГГ-ММ-ДД, например /day 2026-07-10")
            return
    notes = load_day(user_id, day)
    if not notes:
        await update.message.reply_text(f"За {day} записей нет.")
        return
    await update.message.chat.send_action("typing")
    try:
        digest = make_digest(notes)
    except Exception as e:
        log.exception("digest error")
        await update.message.reply_text(f"Не смог собрать сводку: {e}")
        return
    header = f"🗓 <b>Daily dollar balance — {day}</b>\n\n"
    await update.message.reply_text(header + digest, parse_mode="HTML")


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    notes = load_range(str(update.effective_user.id), 7)
    if not notes:
        await update.message.reply_text("За последние 7 дней записей нет.")
        return
    await update.message.chat.send_action("typing")
    try:
        digest = make_digest(notes, label_dates=True)
    except Exception as e:
        log.exception("digest error")
        await update.message.reply_text(f"Не смог собрать сводку: {e}")
        return
    header = f"🗓 <b>Сводка за 7 дней (по {_today()})</b>\n\n"
    await update.message.reply_text(header + digest, parse_mode="HTML")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    days = list_days(str(update.effective_user.id))
    if not days:
        await update.message.reply_text("История пуста.")
        return
    lines = ["📚 Дни с записями:"]
    for d in sorted(days, reverse=True):
        lines.append(f"• {d} — {len(load_day(str(update.effective_user.id), d))} зап.")
    lines.append("\nПосмотреть день: /day ГГГГ-ММ-ДД")
    await update.message.reply_text("\n".join(lines))


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text("Что искать? Напиши, например: /find аренда")
        return
    q = query.lower()
    user_id = str(update.effective_user.id)
    matches = []
    for d in sorted(list_days(user_id), reverse=True):
        for n in load_day(user_id, d):
            hay = ((n.get("transcript") or "") + " " + (n.get("structured") or "")).lower()
            if q in hay:
                matches.append(f"• {d} [{n.get('ts', '')}] {_first_line(n)}")
                if len(matches) >= 20:
                    break
        if len(matches) >= 20:
            break
    if not matches:
        await update.message.reply_text(f"Ничего не найдено по «{query}».")
        return
    await update.message.reply_text(f"🔎 Найдено по «{query}»:\n" + "\n".join(matches))


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

    # Команда правки/удаления записи или обычная заметка?
    notes = load_day(user_id, _today())
    route = route_message(transcript, notes)
    action = route.get("action", "note")

    if action in ("delete", "edit"):
        if not notes:
            await update.message.reply_text("Нет записей за сегодня.")
            return
        idx = _resolve_index(notes, route.get("target"))
        if idx is None:
            await update.message.reply_text(
                "Не понял, какую запись " + ("удалить" if action == "delete" else "править")
                + ". Вот список — уточни номер:\n\n" + format_list(notes)
            )
            return
        num = idx + 1

        if action == "delete":
            removed = delete_at(user_id, num)
            await update.message.reply_text(
                f"🗑 Удалил запись {num}:\n\n{removed['structured']}\n\n"
                "Можешь продиктовать заново."
            )
            return

        # edit
        instruction = (route.get("instruction") or "").strip()
        if not instruction:
            await update.message.reply_text(
                f"Что поправить в записи {num}? Напиши правку "
                "(например «сумма была 500, а не 5000»)."
            )
            return
        note = notes[idx]
        try:
            new_structured = restructure_with_correction(
                note["transcript"], note["structured"], instruction
            )
        except Exception as e:
            log.exception("edit error")
            await update.message.reply_text(f"Не смог применить правку: {e}")
            return
        replace_at(user_id, num, new_structured, instruction)
        await update.message.reply_text(f"✏️ Обновил запись {num}:\n\n" + new_structured)
        return

    # Обычная заметка — структурируем и сохраняем.
    try:
        structured = structure_note(transcript)
    except Exception as e:
        log.exception("structure error")
        await update.message.reply_text(f"Не смог обработать заметку: {e}")
        return

    append_note(user_id, transcript, structured)
    await update.message.reply_text(structured, reply_markup=MAIN_KEYBOARD)


async def _post_init(app: Application) -> None:
    """Регистрирует список команд — он показывается по кнопке «Menu» и по «/»."""
    await app.bot.set_my_commands(
        [
            BotCommand("list", "Список записей за сегодня"),
            BotCommand("undo", "Удалить последнюю запись"),
            BotCommand("day", "Сводка дня (можно /day ГГГГ-ММ-ДД)"),
            BotCommand("week", "Сводка за 7 дней"),
            BotCommand("history", "Дни с записями"),
            BotCommand("find", "Поиск по записям: /find текст"),
            BotCommand("clear", "Очистить заметки за день"),
            BotCommand("start", "Помощь и мой ID"),
        ]
    )


def main():
    if not ALLOWED_USER_IDS:
        log.warning(
            "ALLOWED_USER_ID не задан — бот НИКОГО не пустит. "
            "Узнай свой id через /start и добавь его в переменные окружения."
        )
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("day", day_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    # Нажатия кнопок клавиатуры — до общего обработчика заметок.
    app.add_handler(MessageHandler(filters.Text({BTN_LIST, BTN_DAY, BTN_UNDO, BTN_CLEAR}), buttons))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_note))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_note))
    log.info("Бот запущен (модель: %s).", MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()

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
from datetime import datetime, timezone, timedelta

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
Тебе дают СЕГОДНЯШНЮЮ дату, СПИСОК записей за сегодня (номер + краткая выжимка) и СООБЩЕНИЕ.
Определи намерение. Верни СТРОГО JSON:
{"action": "...", "target": <номер|null>, "instruction": "...", "date": "<ГГГГ-ММ-ДД|null>"}.

action:
- "delete"     — удалить ОДНУ конкретную запись. target = её номер из списка.
- "edit"       — исправить/переделать конкретную запись. target = номер, instruction = суть правки.
- "delete_day" — удалить ВСЕ записи за какой-то день. date = дата в формате ГГГГ-ММ-ДД.
- "note"       — обычная новая заметка (значение по умолчанию).

Как определить date для "delete_day" (ВСЕГДА возвращай ГГГГ-ММ-ДД, это внутренний формат):
- пользователь говорит день-месяц-год: "за 14-07-26" / "за 14-07-2026" / "за 14.07.26"
  → "2026-07-14". Первое число — ДЕНЬ, второе — МЕСЯЦ. Двузначный год 26 = 2026.
- "за сегодня" → СЕГОДНЯ; "за вчера" → день до СЕГОДНЯ; "за 14 июля" → 14 июля текущего года.
- если день понять невозможно — date = null.

Как определить target (номер записи из списка):
- "последнюю" = последний номер; "предпоследнюю" = предпоследний.
- "вторую запись" / "запись 2" = 2; "первую" = 1; и т.д.
- можно ссылаться по смыслу: "исправь запись про аренду" → номер записи, где речь про аренду.
- если это команда правки/удаления, но какую именно запись — непонятно, target = null.

Правила:
- Командой считай только явные инструкции править/удалять запись(и). Простое описание
  событий/фактов — это "note" (target и date null), даже если есть слова «удалил», «исправил».
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
                  "вчера", "сегодня", "день", "дня", "числ",
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
    user = (
        f"СЕГОДНЯ: {_today()}\n\n"
        f"СПИСОК ЗАПИСЕЙ ЗА СЕГОДНЯ:\n{listing}\n\n"
        f"СООБЩЕНИЕ:\n{text}"
    )
    try:
        data = _extract_json(_claude(ROUTER_PROMPT, user, max_tokens=300))
        if data.get("action") in ("delete", "edit", "delete_day", "note"):
            return data
    except Exception:
        log.exception("router error")
    return {"action": "note"}


def make_digest(notes: list, label_dates: bool = False) -> str:
    parts = []
    for n in notes:
        prefix = (
            f"[{_fmt_date(n.get('date', ''))} {n.get('ts', '')}]".strip()
            if label_dates
            else f"[{n.get('ts', '')}]"
        )
        parts.append(f"{prefix}\n{n['structured']}")
    return _claude(DIGEST_PROMPT, "\n\n---\n\n".join(parts), max_tokens=3000)


# ---------- Хранение заметок по дням ----------
# Всё время — московское (UTC+3, перехода на летнее время в РФ нет с 2014).
MSK = timezone(timedelta(hours=3), "MSK")


def _now() -> datetime:
    return datetime.now(MSK)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


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
    today = _now().date()
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
    """Проверяет внутренний формат ISO (ГГГГ-ММ-ДД) — так называются файлы дней."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


# Внутри даты всегда ISO (ГГГГ-ММ-ДД): так имена файлов сортируются по хронологии.
# Пользователь же видит и вводит ДД-ММ-ГГ.
_INPUT_DATE_FORMATS = ("%d-%m-%y", "%d-%m-%Y", "%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d")


def _parse_date(s: str) -> str | None:
    """Дата от пользователя (ДД-ММ-ГГ, ДД.ММ.ГГГГ, ...) -> внутренний ISO или None."""
    s = s.strip()
    for fmt in _INPUT_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _fmt_date(iso: str) -> str:
    """Внутренний ISO -> ДД-ММ-ГГ для показа пользователю."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d-%m-%y")
    except ValueError:
        return iso


def save_day(user_id: str, day: str, notes: list) -> None:
    with open(_day_path(user_id, day), "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)


def append_note(user_id: str, transcript: str, structured: str) -> None:
    day = _today()
    notes = load_day(user_id, day)
    notes.append(
        {
            "ts": _now().strftime("%H:%M"),
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


def delete_day(user_id: str, day: str) -> int:
    """Удаляет ВСЕ записи за указанный день. Возвращает, сколько удалено."""
    count = len(load_day(user_id, day))
    if count:
        os.remove(_day_path(user_id, day))
    return count


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


def _is_yes(text: str) -> bool:
    return text.strip().lower().rstrip(".!") in {
        "да", "ага", "ок", "окей", "давай", "подтверждаю", "удаляй", "yes", "y"
    }


def _is_no(text: str) -> bool:
    return text.strip().lower().rstrip(".!") in {
        "нет", "не", "отмена", "отмени", "не надо", "стоп", "no", "n"
    }


def _first_line(note: dict) -> str:
    s = note.get("structured") or ""
    return s.splitlines()[0] if s else ""


def format_list(notes: list) -> str:
    """Нумерованный список записей: '1. [12:30] 📝 выжимка'."""
    return "\n".join(
        f"{i}. [{n.get('ts', '')}] {_first_line(n)}" for i, n in enumerate(notes, 1)
    )


# ---------- Кнопки ----------
BTN_DAY = "🗓 Сводка дня"
BTN_WEEK = "📅 За неделю"
BTN_LIST = "📋 Список"
BTN_HISTORY = "📚 История"
BTN_FIND = "🔎 Поиск"
BTN_UNDO = "↩️ Удалить последнюю"
BTN_CLEAR = "🧹 Очистить день"
BTN_HELP = "❓ Помощь"

# Сгруппировано по смыслу: сводки / просмотр / поиск и правка / опасное и справка.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_DAY, BTN_WEEK],
        [BTN_LIST, BTN_HISTORY],
        [BTN_FIND, BTN_UNDO],
        [BTN_CLEAR, BTN_HELP],
    ],
    resize_keyboard=True,
)

ALL_BUTTONS = {BTN_DAY, BTN_WEEK, BTN_LIST, BTN_HISTORY, BTN_FIND, BTN_UNDO, BTN_CLEAR, BTN_HELP}


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
        "👋 <b>Голосовой блокнот</b>\n"
        "Надиктовывай голосовые — расшифрую, структурирую и дам выжимку.\n\n"
        "🗓 <b>Сводки</b>\n"
        "/day — за сегодня (или <code>/day 14-07-26</code> за прошлый день)\n"
        "/week — за последние 7 дней\n\n"
        "📋 <b>Записи</b>\n"
        "/list — записи за сегодня, с номерами\n"
        "/history — дни, за которые есть записи\n"
        "/find текст — поиск по всем записям\n\n"
        "✏️ <b>Правка</b>\n"
        "/undo — удалить последнюю запись\n"
        "Голосом: «удали вторую запись», «исправь запись 3: сумма 500»\n\n"
        "🧹 <b>Удаление дня</b>\n"
        "/clear — сегодня (или <code>/clear 14-07-26</code> — любой день)\n"
        "Голосом: «удали все записи за 14-07-26» — переспрошу подтверждение\n\n"
        "Даты — в формате ДД-ММ-ГГ, время московское.\n"
        "Кнопки внизу — быстрый доступ.\n\n"
        f"Твой Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатия кнопок клавиатуры — вызывают соответствующие команды."""
    text = update.message.text
    if text == BTN_DAY:
        return await day_cmd(update, context)
    if text == BTN_WEEK:
        return await week_cmd(update, context)
    if text == BTN_LIST:
        return await list_cmd(update, context)
    if text == BTN_HISTORY:
        return await history_cmd(update, context)
    if text == BTN_FIND:
        return await find_cmd(update, context)
    if text == BTN_UNDO:
        return await undo_cmd(update, context)
    if text == BTN_CLEAR:
        return await clear_cmd(update, context)
    if text == BTN_HELP:
        return await start(update, context)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    notes = load_day(str(update.effective_user.id), _today())
    if not notes:
        await update.message.reply_text("За сегодня пока нет заметок.")
        return
    await update.message.reply_text(
        f"Записи за {_fmt_date(_today())} ({len(notes)}):\n" + format_list(notes)
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
        parsed = _parse_date(args[0])
        if not parsed:
            await update.message.reply_text("Дата в формате ДД-ММ-ГГ, например /day 14-07-26")
            return
        day = parsed
    notes = load_day(user_id, day)
    if not notes:
        await update.message.reply_text(f"За {_fmt_date(day)} записей нет.")
        return
    await update.message.chat.send_action("typing")
    try:
        digest = make_digest(notes)
    except Exception as e:
        log.exception("digest error")
        await update.message.reply_text(f"Не смог собрать сводку: {e}")
        return
    header = f"🗓 <b>Daily dollar balance — {_fmt_date(day)}</b>\n\n"
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
    header = f"🗓 <b>Сводка за 7 дней (по {_fmt_date(_today())})</b>\n\n"
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
        lines.append(f"• {_fmt_date(d)} — {len(load_day(str(update.effective_user.id), d))} зап.")
    lines.append("\nПосмотреть день: /day ДД-ММ-ГГ")
    await update.message.reply_text("\n".join(lines))


def _search(user_id: str, query: str, limit: int = 20) -> list:
    """Ищет подстроку по всем записям за все дни. Возвращает готовые строки."""
    q = query.lower()
    matches = []
    for d in sorted(list_days(user_id), reverse=True):
        for n in load_day(user_id, d):
            hay = ((n.get("transcript") or "") + " " + (n.get("structured") or "")).lower()
            if q in hay:
                matches.append(f"• {_fmt_date(d)} [{n.get('ts', '')}] {_first_line(n)}")
                if len(matches) >= limit:
                    return matches
    return matches


async def _reply_find(update: Update, query: str) -> None:
    matches = _search(str(update.effective_user.id), query)
    if not matches:
        await update.message.reply_text(f"Ничего не найдено по «{query}».")
        return
    await update.message.reply_text(f"🔎 Найдено по «{query}»:\n" + "\n".join(matches))


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    query = " ".join(context.args or []).strip()
    if not query:
        # Нажата кнопка «Поиск» или /find без аргумента — спросим запрос следующим сообщением.
        context.user_data["pending_find"] = True
        await update.message.reply_text("🔎 Что искать? Напиши слово или фразу (или «отмена»).")
        return
    await _reply_find(update, query)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    args = context.args or []
    day = _today()
    if args:
        parsed = _parse_date(args[0])
        if not parsed:
            await update.message.reply_text("Дата в формате ДД-ММ-ГГ, например /clear 14-07-26")
            return
        day = parsed
    count = delete_day(str(update.effective_user.id), day)
    if count == 0:
        await update.message.reply_text(f"За {_fmt_date(day)} записей нет.")
        return
    await update.message.reply_text(f"🗑 Удалил все записи за {_fmt_date(day)} ({count} шт.).")


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

    # Ждём подтверждения удаления целого дня?
    pending = context.user_data.pop("pending_delete_day", None)
    if pending:
        if _is_yes(transcript):
            n = delete_day(user_id, pending)
            await update.message.reply_text(
                f"🗑 Удалил все записи за {_fmt_date(pending)} ({n} шт.)."
            )
            return
        if _is_no(transcript):
            await update.message.reply_text(f"Отменил — записи за {_fmt_date(pending)} на месте.")
            return
        # Ответ не про подтверждение: отменяем удаление и обрабатываем как обычно.
        await update.message.reply_text(f"Отменил удаление за {_fmt_date(pending)}.")

    # Ждём поисковый запрос (нажата кнопка «Поиск»)?
    if context.user_data.pop("pending_find", None):
        if _is_no(transcript):
            await update.message.reply_text("Отменил поиск.")
            return
        await _reply_find(update, transcript.strip())
        return

    # Команда правки/удаления записи или обычная заметка?
    notes = load_day(user_id, _today())
    route = route_message(transcript, notes)
    action = route.get("action", "note")

    if action == "delete_day":
        date = (route.get("date") or "").strip()
        if not _valid_date(date):
            await update.message.reply_text(
                "Не понял, за какой день удалять. Скажи, например: "
                "«удали все записи за 14-07-26», или команду /clear ДД-ММ-ГГ"
            )
            return
        count = len(load_day(user_id, date))
        if count == 0:
            await update.message.reply_text(f"За {_fmt_date(date)} записей нет.")
            return
        context.user_data["pending_delete_day"] = date
        await update.message.reply_text(
            f"⚠️ Удалить ВСЕ записи за {_fmt_date(date)} ({count} шт.)? Отменить будет нельзя.\n"
            "Ответь «да» для подтверждения."
        )
        return

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
            BotCommand("day", "🗓 Сводка дня (можно /day 14-07-26)"),
            BotCommand("week", "📅 Сводка за 7 дней"),
            BotCommand("list", "📋 Записи за сегодня"),
            BotCommand("history", "📚 Дни с записями"),
            BotCommand("find", "🔎 Поиск по записям"),
            BotCommand("undo", "↩️ Удалить последнюю запись"),
            BotCommand("clear", "🧹 Очистить день (можно /clear 14-07-26)"),
            BotCommand("start", "❓ Помощь"),
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
    app.add_handler(MessageHandler(filters.Text(ALL_BUTTONS), buttons))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_note))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_note))
    log.info("Бот запущен (модель: %s).", MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()

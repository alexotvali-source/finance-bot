"""
Telegram-бот для финансового учёта управляющего.

Что делает:
  - принимает текстовые (и по желанию голосовые) сообщения,
  - разбирает их через Claude на операции: приход / расход / выдача /
    возврат / приём в управление / выплата из управления,
  - отправляет операции в Google-таблицу (через Apps Script),
  - в ответ пишет подтверждение и актуальные балансы.

Все ключи и адреса задаются через переменные окружения (см. README и .env.example).
"""

import os
import json
import logging

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
SHEET_WEBHOOK_URL = os.environ["SHEET_WEBHOOK_URL"]      # URL веб-приложения Apps Script
SHEET_SECRET = os.environ["SHEET_SECRET"]               # тот же секрет, что в скрипте
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "") # твой Telegram user id (защита)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")   # опционально, для голосовых
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("finance-bot")
anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------- Промпт для разбора сообщений ----------
SYSTEM_PROMPT = """Ты — парсер финансовых операций управляющего. На вход приходит
свободный текст на русском о движении денег. Верни СТРОГО JSON без пояснений.

Формат:
{
  "operations": [
    {
      "type": "приход|расход|выдача|возврат|приём_в_управление|выплата_управления",
      "currency": "USD|RUB",
      "amount": <число, всегда положительное>,
      "counterparty": "<имя человека или источник, если есть; иначе пусто>",
      "comment": "<краткое назначение>"
    }
  ],
  "note": "<короткий комментарий пользователю на русском>"
}

Правила определения типа:
- "приход": деньги пришли ко мне (доход, поступление, занёс, прислали).
- "расход": я потратил (купил, оплатил, потратил).
- "выдача": я выдал кому-то деньги, и он теперь мне должен (выдал, дал в долг).
- "возврат": мне вернули ранее выданное (вернул долг, отдал).
- "приём_в_управление": инвестор/человек дал мне деньги в управление.
- "выплата_управления": я вернул инвестору его деньги из управления.

Валюта: "доллары/баксы/usd/$" -> USD; "рубли/руб/₽/р" -> RUB.
Если валюта не указана явно, но сумма в рублях по контексту — RUB; иначе спрашивать не нужно,
ставь наиболее вероятную и упомяни это в note.
В одном сообщении может быть несколько операций — верни их все.
Если это не операция (вопрос, приветствие), верни "operations": [] и заполни note.
"""


def parse_message(text: str) -> dict:
    """Разбирает текст в операции через Claude."""
    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip()
    # На случай, если модель обернёт в ```json
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):]
    data = json.loads(raw)
    for op in data.get("operations", []):
        op["raw"] = text
    return data


def send_to_sheet(operations: list) -> dict:
    """Отправляет операции в Google-таблицу, возвращает балансы."""
    r = requests.post(
        SHEET_WEBHOOK_URL,
        json={"secret": SHEET_SECRET, "operations": operations},
        timeout=30,
    )
    return r.json()


def get_balances() -> dict:
    r = requests.get(SHEET_WEBHOOK_URL, params={"secret": SHEET_SECRET}, timeout=30)
    return r.json()


def transcribe_voice(file_path: str) -> str:
    """Опционально: расшифровка голосового через OpenAI Whisper (нужен OPENAI_API_KEY)."""
    if not OPENAI_API_KEY:
        return ""
    with open(file_path, "rb") as f:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": f},
            data={"model": "whisper-1", "language": "ru"},
            timeout=120,
        )
    return r.json().get("text", "")


def format_balances(balances: dict) -> str:
    """Красиво форматирует балансы для ответа."""
    if not balances:
        return ""
    lines = ["\n📊 <b>Текущие балансы</b>"]
    cash = balances.get("cash", {})
    lines.append(f"Касса: {fmt(cash.get('USD', 0))} $ | {fmt(cash.get('RUB', 0))} ₽")

    mgmt = [m for m in balances.get("mgmt", []) if m.get("usd") or m.get("rub")]
    if mgmt:
        lines.append("\n💼 <b>В управлении</b>")
        for m in mgmt:
            lines.append(f"• {m['name']}: {fmt(m['usd'])} $ | {fmt(m['rub'])} ₽")

    debt = [d for d in balances.get("debt", []) if d.get("usd") or d.get("rub")]
    if debt:
        lines.append("\n🤝 <b>Долги (+ мне должны / − я должен)</b>")
        for d in debt:
            lines.append(f"• {d['name']}: {fmt(d['usd'])} $ | {fmt(d['rub'])} ₽")
    return "\n".join(lines)


def fmt(x) -> str:
    try:
        n = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{n:,.0f}".replace(",", " ")


def allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_USER_ID)


# ---------- Хендлеры ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я веду твой финансовый учёт.\n\n"
        "Просто пиши операции обычным текстом, например:\n"
        "• «приход 5000 долларов от Игоря»\n"
        "• «потратил 3000 рублей на рекламу»\n"
        "• «выдал Диме 500 баксов на закуп»\n"
        "• «Саша занёс 10000 долларов в управление»\n\n"
        "Команда /balance — показать текущие балансы.\n"
        f"Твой user id: {update.effective_user.id}"
    )


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    res = get_balances()
    if res.get("ok"):
        await update.message.reply_text(
            format_balances(res.get("balances", {})) or "Пока нет данных.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("Не удалось получить балансы 😕")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return

    text = update.message.text

    # Голосовые (опционально)
    if update.message.voice:
        if not OPENAI_API_KEY:
            await update.message.reply_text(
                "Голосовые пока не подключены. Напиши операцию текстом "
                "или добавь OPENAI_API_KEY (см. README)."
            )
            return
        tg_file = await update.message.voice.get_file()
        path = f"/tmp/{update.message.voice.file_id}.ogg"
        await tg_file.download_to_drive(path)
        text = transcribe_voice(path)
        if not text:
            await update.message.reply_text("Не смог расшифровать голосовое 😕")
            return

    try:
        parsed = parse_message(text)
    except Exception as e:
        log.exception("parse error")
        await update.message.reply_text(f"Не смог разобрать сообщение: {e}")
        return

    ops = parsed.get("operations", [])
    if not ops:
        await update.message.reply_text(parsed.get("note") or "Это не похоже на операцию.")
        return

    res = send_to_sheet(ops)
    if not res.get("ok"):
        await update.message.reply_text(f"Ошибка записи в таблицу: {res.get('error')}")
        return

    # Собираем подтверждение
    lines = ["✅ Записал:"]
    for op in ops:
        lines.append(
            f"• {op['type']} {fmt(op['amount'])} {op['currency']}"
            + (f" — {op['counterparty']}" if op.get("counterparty") else "")
            + (f" ({op['comment']})" if op.get("comment") else "")
        )
    if parsed.get("note"):
        lines.append(f"\nℹ️ {parsed['note']}")
    lines.append(format_balances(res.get("balances", {})))

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_text))
    log.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()

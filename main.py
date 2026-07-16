"""
Telegram-бот — реестр денег Ильи, плюс голосовые заметки.

Деньги (главное):
  голос/текст -> Whisper -> Claude ИНТЕРПРЕТИРУЕТ сказанное -> превью -> «да» ->
  запись в Google-таблицу (она и есть правда) + строка в журнал.

Три вещи, которые тут держат всё:
  1. Модель НИКОГДА не считает. Она только называет услышанную сумму и корзину;
     вычитание и итоги — в ledger.py. На арифметике и классификации она уже
     ошибалась: 94 000 не в ту корзину, дебиторка внутрь «под управлением».
  2. Балансы Илья даёт СНИМКАМИ, а не событиями («операционный баланс 2 213 258»).
     Поэтому расход по умолчанию баланс не трогает: снимок его всё равно затрёт.
  3. Ошибки не глушим. Не разобрали — говорим об этом; реестр остаётся как был.

Заметки — отдельно и проще: расшифровал, структурировал, сложил по дням.
Все ключи — через переменные окружения (см. .env.example и README).
"""

from __future__ import annotations

import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta

import requests
import ledger
from anthropic import Anthropic
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------- Настройки из переменных окружения ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]           # для расшифровки голосовых (Whisper)
NOTES_DIR = os.environ.get("NOTES_DIR", "notes")

# Модель зашита намеренно: переменная окружения могла бы молча подменить её
# на более слабую. Бот всегда работает на Opus 4.8.
MODEL = "claude-opus-4-8"

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

LEDGER_PROMPT = """Ты превращаешь фразу Ильи о деньгах в изменения реестра.
Тебе дают ТЕКУЩИЙ РЕЕСТР (позиция: сумма) и СООБЩЕНИЕ.

КАК ГОВОРИТ ИЛЬЯ: обычно он называет НОВЫЕ ОСТАТКИ, а не события.
«Макс 200 000» = теперь у Макса 200 000. «Стефан 325 168, Вадим 80 000» = новые
остатки этих позиций. Это "set" — просто передай названную сумму.
Реже он говорит про изменение: «добавил наличкой 14 000» — это "add".

Верни СТРОГО JSON:
{
  "is_money": true|false,
  "kind": "balance"|"expense",
  "summary": "<что происходит, одной строкой по-человечески>",
  "set": [{"path": "<путь>", "amount": <НОВЫЙ остаток, как назвал Илья>}],
  "add": [{"path": "<путь>", "amount": <изменение: + или ->}],
  "correction": true|false,
  "expense": {
    "date": "<ГГГГ-ММ-ДД, когда потрачено; не сказано — null>",
    "period": "<за какой период, словами Ильи: «до 15.03», «15.03–13.07»; иначе пусто>",
    "by_person": {"<Имя>": {"rub": <сумма>, "usd": <сумма>}},
    "total_rub": <итого рублей>, "total_usd": <итого долларов>,
    "covered_by_profit_rub": <если сказал, что часть покрыта прибылью>,
    "paid_from_working": {"rub": <сколько ушло с рабочего баланса>,
                          "usd": <та же сумма в долларах, если Илья её назвал>},
    "note": "<на что потрачено, коротко>"
  },
  "deduct": true|false,
  "question": "<если непонятно, о какой позиции речь — что спросить; иначе пусто>"
}

KIND — что это вообще:
- "balance" — названы ОСТАТКИ или изменения позиций. Заполняй set/add, expense = null.
- "expense" — Илья ПОТРАТИЛ деньги: «потратил 300 000 на офис», «расходы за июль:
  Илья 2 951 930, Дмитрий 10 527 758». Заполняй expense, set/add оставь пустыми.
- "delete_expense" — Илья отменяет расход: «удали эту операцию», «удали последний
  расход», «отмени расход». Больше ничего не заполняй — программа покажет,
  какой расход удаляет, и переспросит.

ИМЯ В РАСХОДЕ ТЕРЯТЬ НЕЛЬЗЯ. Если в фразе названо имя — оно ОБЯЗАНО попасть
в by_person, даже когда человек один:
- «добавь расход Илья 10 000 долларов» → by_person: {"Илья": {"usd": 10000}},
  total_usd: 10000. НЕ оставляй by_person пустым: накопительные итоги считаются
  по именам, безымянный расход Илье не прибавится.
- «общий расход 15 000» без имён → by_person пустой, только total. Это норма.
- «с пометкой купил старкнет» → note: "купил Старкнет".

DEDUCT — трогать ли рабочий баланс. По умолчанию FALSE: Илья называет балансы
снимками, и снимок затрёт списание. true — ТОЛЬКО если он прямо сказал списать:
«спиши с рабочего баланса», «отними с операционного баланса», «вычти из общих».
(Списание своих трат всегда идёт из рабочего баланса — это наша часть
операционного кошелька, отдельного пути нет.) Сомневаешься — false.
Если deduct=true, а сумма только в рублях — задай question про доллары:
реестр долларовый, курс придумывать нельзя.

Суммы в expense передавай КАК УСЛЫШАЛ, по каждому человеку отдельно. Итоги тоже
назови, но НЕ считай их сам — просто повтори то, что сказал Илья; сходимость
проверит программа и переспросит, если не сойдётся.

CORRECTION — правка неверно записанной цифры, а НЕ движение денег. true, если Илья
говорит, что данные ошибочны: «это ошибка в данных», «я неверно продиктовал»,
«поправь, там должно быть», «не движение, просто исправь цифру».
Деньги при этом никуда не шли — бот не назовёт разницу прибылью или расходом.
По умолчанию false: обычная операция. Не помечай правкой то, где деньги реально
двигались, даже если сумма странная.

САМОЕ ВАЖНОЕ: НИКОГДА не считай разницу и не складывай сам. Для "set" просто
передай названную сумму как есть. Вычитание, сложение и все итоги делает программа —
у тебя на этом бывают ошибки, поэтому арифметика не твоя работа.

НОВЫЕ ЛЮДИ — это нормально и бывает часто. Реестр в сообщении показан лишь для того,
чтобы ты знал текущие суммы, а НЕ как список допустимых имён.
- «нам внесли деньги партнёры: Иван 50 000, Пётр 30 000» → wallet.held.Иван = 50000
  и wallet.held.Пётр = 30000, даже если этих имён в реестре нет. Просто новый путь.
- «Сергей занёс 100 000» → wallet.held.Сергей.
- НЕ отказывайся и НЕ задавай question только потому, что имени пока нет в реестре.
  Имя названо — этого достаточно.

ОПЕРАЦИОННЫЙ БАЛАНС — это НЕ рабочий баланс. Это ВЕСЬ кошелёк целиком:
рабочий баланс + ВСЕ деньги в управлении. Илья называет чаще всего именно его.
- «операционный баланс 2 213 258» / «операционный кошелёк 2 213 258» / «по операционке
  2 213 258» → set path "wallet.operational", amount 2213258.
  Программа сама вычтет чужие деньги и получит рабочий баланс. Ты НЕ вычитаешь.
- «рабочий баланс 265 484» → wallet.working. Это другая цифра, не путай их.
Ошибка тут дорогая: положив операционный баланс в рабочий, ты объявишь чужие
деньги прибылью Ильи.

ПУТИ (бери существующие; новое имя человека или актива — можно, просто новый путь):
- wallet.operational    — ВИРТУАЛЬНЫЙ путь: весь кошелёк (рабочий + в управлении).
  Только для "set", только когда названа сумма всего операционного баланса.
- wallet.working        — рабочий баланс: общие свободные деньги Ильи и Дмитрия.
  Сюда приходит прибыль и отсюда уходят расходы.
- wallet.held.<Имя>     — деньги в управлении: ЧУЖИЕ деньги, лежат у нас.
  Сюда кладём, только если Илья назвал имя («это деньги Макса»).
  Прибыль сюда НИКОГДА не попадает — она не чужая.
- assets.<Название>     — наши активы вне кошелька (Крипта, Наличка, Заморожено...)
- receivables.<Имя>     — нам должны (дебиторка)

ЧЕГО НЕ НАДО СПРАШИВАТЬ:
- Откуда взялась прибыль. Рост рабочего баланса — это прибыль, Илья ведёт её источник
  отдельно и говорить о нём не будет. Просто прими новый остаток.
- Куда ушли деньги при уменьшении, если он сам не сказал. Программа покажет изменение,
  Илья увидит его глазами и решит сам.

ЧТО СПРОСИТЬ (question) — только если непонятно САМО СООБЩЕНИЕ:
- неясно, о какой позиции или о каком человеке речь;
- сумма названа в рублях (реестр долларовый) — спроси, сколько это в долларах.

Не про деньги (мысли, планы, встречи) — is_money: false, списки пустые.
Сомневаешься — is_money: false."""

ROUTER_PROMPT = """Пользователь ведёт нумерованные голосовые заметки за день.
Тебе дают СЕГОДНЯШНЮЮ дату, СПИСОК записей за сегодня (номер + краткая выжимка) и СООБЩЕНИЕ.
Определи намерение. Верни СТРОГО JSON:
{"action": "...", "target": <номер|null>, "instruction": "...", "date": "<ГГГГ-ММ-ДД|null>"}.

action:
- "delete"     — удалить ОДНУ конкретную запись. target = её номер из списка.
- "edit"       — исправить/переделать конкретную запись. target = номер, instruction = суть правки.
- "delete_day" — удалить ВСЕ записи за какой-то день. date = дата в формате ГГГГ-ММ-ДД.
- "note"       — обычная новая заметка (значение по умолчанию). Если в сообщении указана
  дата, за какой день эта запись — верни её в date. Примеры: «14.06.26 внёс в сейф
  наличные» → date = "2026-06-14"; «вчера забрал у Макса 250 000» → date = вчерашний день.
  Если даты нет — date = null (запись пойдёт за сегодня).

Как определить date (ВСЕГДА возвращай ГГГГ-ММ-ДД, это внутренний формат):
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
  событий/фактов — это "note", даже если есть слова «удалил», «исправил».
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
def _claude(system: str, user: str, max_tokens: int = 4096) -> str:
    # Адаптивное мышление: модель сама решает, сколько думать. Без него Opus 4.8
    # склонен вписывать рассуждения прямо в видимый ответ — а он идёт в заметку.
    # Лимит max_tokens покрывает мышление + ответ, поэтому он с запасом.
    resp = anthropic.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    # Мышление считается в max_tokens и может съесть весь лимит: тогда ответ
    # обрывается, текста нет — и раньше это молча превращалось в заметку.
    # Пусть лучше падает громко.
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"ответ обрезан лимитом токенов ({max_tokens})")
    # Берём только текстовые блоки — блоки мышления сюда не попадают.
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


def mentions_ledger(text: str) -> bool:
    """Фраза про деньги, а не про заметки. Такие сообщения роутер заметок видеть
    не должен: «удали последний расход» он понимал как удаление записи дневника
    и удалял её. И в денежную ветку такие фразы идут даже без цифр —
    в «удали последний расход» цифр нет вовсе."""
    t = text.lower()
    return any(w in t for w in ("расход", "баланс", "реестр", "управлени",
                                "дебитор", "актив", "кошел", "спиши", "списа"))


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


# Дата в сообщении: цифрами (14.06.26) или словами («10 июня», «вчера») —
# голосом дату чаще диктуют словами, поэтому одних цифр мало.
_MONTHS = r"январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр"
_DATE_RE = re.compile(
    "|".join(
        (
            r"\d{1,2}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{2,4}",  # 14.06.26, 10-06-2026
            r"\b(?:" + _MONTHS + r")",                          # «10 июня», «июня»
            r"\b(?:вчера|позавчера|прошл)",                     # «вчера», «на прошлой неделе»
        )
    ),
    re.IGNORECASE,
)


def looks_like_dated(text: str) -> bool:
    """Похоже ли, что в сообщении указана дата (значит, запись может быть за прошлый день)."""
    return bool(_DATE_RE.search(text))


def interpret_money(text: str, book: dict) -> dict:
    """Превращает фразу о деньгах в изменения реестра. Модель называет суммы,
    арифметику (дельты, итоги) делает Python.

    Ошибки НЕ глушим: раньше любой сбой молча возвращал is_money=False, и вместо
    операции появлялась заметка — без единого намёка, что что-то пошло не так.
    Пусть лучше бот скажет, что не смог.
    """
    current = "\n".join(f"{p}: {ledger.fmt(ledger.get(book, p))}" for p in ledger.paths(book))
    user = f"ТЕКУЩИЙ РЕЕСТР:\n{current}\n\nСООБЩЕНИЕ:\n{text}"
    # Лимит с запасом: мышление считается в него же.
    return _extract_json(_claude(LEDGER_PROMPT, user, max_tokens=8000))


def route_message(text: str, notes: list) -> dict:
    """Классифицирует сообщение: заметка (возможно за прошлый день) или правка/удаление.
    Роутер видит нумерованный список записей и сегодняшнюю дату."""
    if not (looks_like_edit(text) or looks_like_dated(text)):
        return {"action": "note"}
    listing = format_list(notes) or "(записей нет)"
    user = (
        f"СЕГОДНЯ: {_today()}\n\n"
        f"СПИСОК ЗАПИСЕЙ ЗА СЕГОДНЯ:\n{listing}\n\n"
        f"СООБЩЕНИЕ:\n{text}"
    )
    try:
        data = _extract_json(_claude(ROUTER_PROMPT, user, max_tokens=2048))
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
    return _claude(DIGEST_PROMPT, "\n\n---\n\n".join(parts), max_tokens=8000)


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


def append_note(user_id: str, transcript: str, structured: str, day: str | None = None) -> None:
    """Добавляет запись в указанный день (по умолчанию сегодня).
    ts — время внесения записи, а не события: для записи задним числом это нормально."""
    day = day or _today()
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


def delete_at(user_id: str, target, day: str | None = None) -> dict | None:
    """Удаляет запись по 1-based номеру за указанный день (по умолчанию сегодня)."""
    day = day or _today()
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


def replace_at(user_id: str, target, new_structured: str, instruction: str,
               day: str | None = None) -> dict | None:
    """Заменяет структуру записи по 1-based номеру за указанный день. Возвращает её (или None)."""
    day = day or _today()
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
BTN_LEDGER = "📊 Реестр"
BTN_EXPENSES = "🧾 Расходы"
BTN_JOURNAL = "📔 Журнал"
BTN_DAY = "🗓 Сводка дня"
BTN_WEEK = "📅 За неделю"
BTN_LIST = "📋 Список"
BTN_HISTORY = "📚 История"
BTN_FIND = "🔎 Поиск"
BTN_UNDO = "↩️ Удалить последнюю"
BTN_CLEAR = "🧹 Очистить день"
BTN_HELP = "❓ Помощь"

# Первая строка — деньги: реестр (где что лежит), расходы (что потрачено),
# журнал (что менялось). Это три разных вопроса, и ради них бот и существует.
# Вторая и третья — заметки: сводки и просмотр. Четвёртая — правка, пятая — опасное.
# Удаление держим внизу и отдельно от просмотра, чтобы не попасть по нему мимо.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_LEDGER, BTN_EXPENSES, BTN_JOURNAL],
        [BTN_DAY, BTN_WEEK],
        [BTN_LIST, BTN_HISTORY, BTN_FIND],
        [BTN_UNDO, BTN_CLEAR],
        [BTN_HELP],
    ],
    resize_keyboard=True,
)

ALL_BUTTONS = {
    BTN_LEDGER, BTN_EXPENSES, BTN_JOURNAL, BTN_DAY, BTN_WEEK, BTN_LIST,
    BTN_HISTORY, BTN_FIND, BTN_UNDO, BTN_CLEAR, BTN_HELP,
}


# ---------- Inline-клавиатуры (выбор дня и правка записей) ----------
# В callback_data всегда кладём дату вместе с номером: список мог быть открыт вчера,
# а нажат сегодня — по одному номеру попали бы не в ту запись.
def _day_label(iso: str) -> str:
    if iso == _today():
        return "Сегодня"
    if iso == (_now().date() - timedelta(days=1)).strftime("%Y-%m-%d"):
        return "Вчера"
    return _fmt_date(iso)


def _days_keyboard(user_id: str, limit: int = 8) -> InlineKeyboardMarkup | None:
    """Кнопки с днями, за которые есть записи (свежие сверху)."""
    days = sorted(list_days(user_id), reverse=True)[:limit]
    if not days:
        return None
    btns = [
        InlineKeyboardButton(
            f"{_day_label(d)} ({len(load_day(user_id, d))})", callback_data=f"day:{d}"
        )
        for d in days
    ]
    return InlineKeyboardMarkup([btns[i:i + 2] for i in range(0, len(btns), 2)])


def _notes_keyboard(iso: str, notes: list) -> InlineKeyboardMarkup:
    """Кнопки-номера записей: тапнул номер — увидишь запись и действия с ней."""
    btns = [
        InlineKeyboardButton(str(i), callback_data=f"note:{iso}:{i}")
        for i in range(1, len(notes) + 1)
    ]
    return InlineKeyboardMarkup([btns[i:i + 5] for i in range(0, len(btns), 5)])


def _note_actions(iso: str, num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{iso}:{num}"),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{iso}:{num}"),
            ],
            [InlineKeyboardButton("◀️ К списку", callback_data=f"list:{iso}")],
        ]
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
        "💰 <b>Деньги — просто говори цифры</b>\n"
        "«операционный баланс 2 213 258» — весь кошелёк; рабочий баланс посчитаю сам, "
        "вычтя деньги в управлении\n"
        "«баланс Макса 973 406» — деньги в управлении, имя можно новое\n"
        "«потратил 300 000 на офис» — запишу в расходы, баланс НЕ трону\n"
        "«...спиши с рабочего баланса» — тогда трону, но спрошу сумму в долларах\n"
        "«это правка данных, я неверно продиктовал» — не назову это прибылью/расходом\n"
        "Всегда показываю превью и жду «да».\n\n"
        "📊 <b>Смотреть</b>\n"
        "/balance — реестр: где что лежит и сколько всего наше\n"
        "/expenses — расходы: что потрачено и списано ли с баланса\n"
        "/journal — журнал: что менялось в реестре, когда и почему\n\n"
        "👋 <b>Заметки</b>\n"
        "Надиктовывай голосовые — расшифрую, структурирую и дам выжимку.\n"
        "Назови дату — запишу задним числом: «10.06.26 забрал у Макса 250 000».\n\n"
        "🗓 <b>Сводки</b>\n"
        "Кнопка «Сводка дня» — предложит выбрать день\n"
        "/day — за сегодня (или <code>/day 14-07-26</code> за прошлый день)\n"
        "/week — за последние 7 дней\n\n"
        "📋 <b>Записи</b>\n"
        "/list — записи за сегодня; нажми номер → ✏️ Изменить / 🗑 Удалить\n"
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


async def _send_digest(message, user_id: str, iso: str) -> None:
    notes = load_day(user_id, iso)
    if not notes:
        await message.reply_text(f"За {_fmt_date(iso)} записей нет.")
        return
    await message.chat.send_action("typing")
    try:
        digest = make_digest(notes)
    except Exception as e:
        log.exception("digest error")
        await message.reply_text(f"Не смог собрать сводку: {e}")
        return
    await message.reply_text(
        f"🗓 <b>Daily dollar balance — {_fmt_date(iso)}</b>\n\n" + digest, parse_mode="HTML"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатия inline-кнопок: выбор дня, просмотр записи, правка, удаление."""
    q = update.callback_query
    if not allowed(update):
        await q.answer("Доступ только для владельца бота.", show_alert=True)
        return
    await q.answer()

    user_id = str(update.effective_user.id)
    parts = (q.data or "").split(":")
    kind = parts[0]

    if kind == "day":
        await _send_digest(q.message, user_id, parts[1])
        return

    if kind == "list":
        iso = parts[1]
        notes = load_day(user_id, iso)
        if not notes:
            await q.message.reply_text(f"За {_fmt_date(iso)} записей нет.")
            return
        await q.message.reply_text(
            f"Записи за {_fmt_date(iso)} ({len(notes)}):\n" + format_list(notes),
            reply_markup=_notes_keyboard(iso, notes),
        )
        return

    iso, num = parts[1], int(parts[2])
    notes = load_day(user_id, iso)
    idx = _resolve_index(notes, num)
    if idx is None:
        await q.message.reply_text("Этой записи уже нет — открой список заново.")
        return

    if kind == "note":
        n = notes[idx]
        await q.message.reply_text(
            f"Запись {num} — {_fmt_date(iso)} [{n.get('ts', '')}]\n\n{n['structured']}",
            reply_markup=_note_actions(iso, num),
        )
        return

    if kind == "del":
        removed = delete_at(user_id, num, day=iso)
        await q.message.reply_text(
            f"🗑 Удалил запись {num} за {_fmt_date(iso)}:\n\n{removed['structured']}\n\n"
            "Можешь продиктовать заново."
        )
        return

    if kind == "edit":
        context.user_data["pending_edit"] = (iso, num)
        await q.message.reply_text(
            f"✏️ Что поправить в записи {num} за {_fmt_date(iso)}?\n"
            "Напиши или надиктуй правку (или «отмена»)."
        )
        return


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажатия кнопок клавиатуры — вызывают соответствующие команды."""
    text = update.message.text
    if text == BTN_LEDGER:
        return await balance_cmd(update, context)
    if text == BTN_EXPENSES:
        return await expenses_cmd(update, context)
    if text == BTN_JOURNAL:
        return await journal_cmd(update, context)
    if text == BTN_DAY:
        return await day_picker(update, context)
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
    day = _today()
    notes = load_day(str(update.effective_user.id), day)
    if not notes:
        await update.message.reply_text("За сегодня пока нет заметок.")
        return
    await update.message.reply_text(
        f"Записи за {_fmt_date(day)} ({len(notes)}):\n"
        + format_list(notes)
        + "\n\nНажми номер, чтобы изменить или удалить запись.",
        reply_markup=_notes_keyboard(day, notes),
    )


async def _read_ledger(update: Update, user_id: str):
    """Читает реестр. При сбое таблицы честно говорит об этом и возвращает None:
    показать устаревшую цифру хуже, чем не показать никакой."""
    try:
        return ledger.read(NOTES_DIR, user_id)
    except Exception as e:
        log.exception("ledger read error")
        await update.message.reply_text(
            f"⚠️ Не смог прочитать реестр из таблицы: {e}\n\n"
            "Цифры не покажу — они могут быть устаревшими. Попробуй ещё раз."
        )
        return None


async def _write_ledger(update: Update, user_id: str, book: dict,
                        entries: list | None = None) -> bool:
    """Пишет реестр. Не смог — говорит прямо: изменение НЕ применено."""
    try:
        ledger.write(NOTES_DIR, user_id, book, entries)
        return True
    except Exception as e:
        log.exception("ledger write error")
        await update.message.reply_text(
            f"⚠️ Не смог записать в таблицу: {e}\n\nИзменение НЕ применено — реестр как был."
        )
        return False


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Реестр: кто чем владеет и сколько где лежит. Итоги считает Python, не модель."""
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    book = await _read_ledger(update, str(update.effective_user.id))
    if book is None:
        return
    await update.message.reply_text(
        ledger.format_balance(book, _fmt_date),
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def journal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Журнал: что менялось в реестре, когда и почему."""
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    try:
        rows = ledger.read_journal(NOTES_DIR, str(update.effective_user.id))
    except Exception as e:
        # Журнал — про правду; молча показать пустоту вместо истории нельзя.
        log.exception("journal read error")
        await update.message.reply_text(f"⚠️ Не смог прочитать журнал: {e}")
        return
    await update.message.reply_text(
        ledger.format_journal(rows, _fmt_date),
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def expenses_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Журнал расходов: когда и сколько потрачено. Рубли — описание, доллары бьют по балансу."""
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    book = await _read_ledger(update, str(update.effective_user.id))
    if book is None:
        return
    await update.message.reply_text(
        ledger.format_expenses(book, _fmt_date),
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def day_picker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Сводка дня» — предлагает выбрать день из тех, где есть записи."""
    if not allowed(update):
        await update.message.reply_text("Доступ только для владельца бота.")
        return
    kb = _days_keyboard(str(update.effective_user.id))
    if kb is None:
        await update.message.reply_text("Записей пока нет.")
        return
    await update.message.reply_text("🗓 За какой день собрать сводку?", reply_markup=kb)


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

    # Ждём подтверждения удаления расхода?
    if context.user_data.pop("pending_delete_expense", None):
        if _is_yes(transcript):
            before = await _read_ledger(update, user_id)
            if before is None:
                return
            res = ledger.remove_last_expense(before, _today())
            if res is None:
                await update.message.reply_text("Расходов уже нет — удалять нечего.")
                return
            book, rec, refund = res
            entries = []
            if refund:
                # Возврат меняет рабочий баланс — обязан лечь в журнал.
                entries = ledger.log_entries(
                    before, book, [{"path": "wallet.working", "amount": refund}],
                    f"Отмена расхода: {ledger.describe_expense(rec)}", False,
                    _now().strftime("%Y-%m-%d %H:%M"),
                )
            if not await _write_ledger(update, user_id, book, entries):
                return
            await update.message.reply_text(
                "✅ Удалил.\n\n" + ledger.format_balance(book, _fmt_date),
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        if _is_no(transcript):
            await update.message.reply_text("Отменил — расход остался на месте.")
            return
        await update.message.reply_text("Отменил удаление — расход остался на месте.")

    # Ждём подтверждения расхода?
    pending_expense = context.user_data.pop("pending_expense", None)
    if pending_expense:
        if _is_yes(transcript):
            before = await _read_ledger(update, user_id)
            if before is None:
                return
            rec, deduct = pending_expense["rec"], pending_expense["deduct"]
            book = ledger.add_expense(before, rec, deduct, _today())
            entries = []
            if deduct:
                # Списание меняет рабочий баланс — значит обязано быть в журнале.
                # Иначе цифра уедет, а следа не останется.
                usd = ledger.deduct_usd(rec)
                entries = ledger.log_entries(
                    before, book, [{"path": "wallet.working", "amount": -usd}],
                    f"Расход: {rec.get('note') or 'без описания'}", False,
                    _now().strftime("%Y-%m-%d %H:%M"),
                )
            if not await _write_ledger(update, user_id, book, entries):
                return
            await update.message.reply_text(
                "✅ Записал в расходы.\n\n" + ledger.format_balance(book, _fmt_date),
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        if _is_no(transcript):
            await update.message.reply_text("Отменил — расход не записан.")
            return
        await update.message.reply_text("Отменил расход — ничего не записано.")

    # Ждём подтверждения операции над реестром?
    pending_ledger = context.user_data.pop("pending_ledger", None)
    meta = context.user_data.pop("pending_ledger_meta", None) or {}
    if pending_ledger:
        if _is_yes(transcript):
            before = await _read_ledger(update, user_id)
            if before is None:
                return
            book = ledger.apply(before, pending_ledger, _today())
            entries = ledger.log_entries(
                before, book, pending_ledger,
                meta.get("summary") or "Изменение реестра",
                bool(meta.get("correction")),
                _now().strftime("%Y-%m-%d %H:%M"),
            )
            if not await _write_ledger(update, user_id, book, entries):
                return
            await update.message.reply_text(
                "✅ Применил.\n\n" + ledger.format_balance(book, _fmt_date),
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        if _is_no(transcript):
            await update.message.reply_text("Отменил — реестр не тронут.")
            return
        # Ответ не про подтверждение: операцию отменяем, сообщение обрабатываем как обычно.
        await update.message.reply_text("Отменил операцию — реестр не тронут.")

    # Ждём поисковый запрос (нажата кнопка «Поиск»)?
    if context.user_data.pop("pending_find", None):
        if _is_no(transcript):
            await update.message.reply_text("Отменил поиск.")
            return
        await _reply_find(update, transcript.strip())
        return

    # Ждём правку конкретной записи (нажата кнопка «✏️ Изменить»)?
    pending_edit = context.user_data.pop("pending_edit", None)
    if pending_edit:
        iso, num = pending_edit
        if _is_no(transcript):
            await update.message.reply_text("Отменил правку.")
            return
        day_notes = load_day(user_id, iso)
        idx = _resolve_index(day_notes, num)
        if idx is None:
            await update.message.reply_text("Этой записи уже нет.")
            return
        instruction = transcript.strip()
        note = day_notes[idx]
        try:
            new_structured = restructure_with_correction(
                note["transcript"], note["structured"], instruction
            )
        except Exception as e:
            log.exception("edit error")
            await update.message.reply_text(f"Не смог применить правку: {e}")
            return
        replace_at(user_id, num, new_structured, instruction, day=iso)
        await update.message.reply_text(
            f"✏️ Обновил запись {num} за {_fmt_date(iso)}:\n\n" + new_structured
        )
        return

    # Голое «да»/«нет» без ожидающей операции — это не заметка. Раньше такое
    # записывалось пустой записью («распознано только слово „да“») и замусоривало день.
    if _is_yes(transcript) or _is_no(transcript):
        await update.message.reply_text(
            "Сейчас нечего подтверждать или отменять — операция уже завершена."
        )
        return

    # Команда правки/удаления записи или обычная заметка?
    # Фразы про деньги мимо роутера заметок: «удали последний расход» — это
    # про реестр, а не про запись дневника.
    notes = load_day(user_id, _today())
    route = {"action": "note"} if mentions_ledger(transcript) \
        else route_message(transcript, notes)
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

    # Про деньги? Тогда это операция над реестром, а не заметка.
    # Цифры ИЛИ денежные слова: «удали последний расход» — без единой цифры.
    if any(ch.isdigit() for ch in transcript) or mentions_ledger(transcript):
        book = await _read_ledger(update, user_id)
        if book is None:
            return
        try:
            money = interpret_money(transcript, book)
        except Exception as e:
            # Молчать нельзя: иначе вместо операции появится заметка, а Илья
            # будет гадать, почему баланс не изменился.
            log.exception("ledger interpret error")
            await update.message.reply_text(
                f"⚠️ Не смог разобрать операцию: {e}\n\n"
                "Реестр не тронут. Скажи иначе или напиши текстом."
            )
            return
        if money.get("is_money"):
            if money.get("question"):
                await update.message.reply_text(f"❓ {money['question']}")
                return

            if money.get("kind") == "delete_expense":
                book_now = await _read_ledger(update, user_id)
                if book_now is None:
                    return
                res = ledger.remove_last_expense(book_now, _today())
                if res is None:
                    await update.message.reply_text("Расходов нет — удалять нечего.")
                    return
                _, rec, refund = res
                msg = f"🗑 Удаляю расход: <b>{ledger.describe_expense(rec)}</b>"
                if refund:
                    w = ledger.get(book_now, "wallet.working")
                    msg += (f"\n↩️ Возвращаю на рабочий баланс <b>{ledger.fmt(refund)} $</b>"
                            f"\nРабочий баланс: {ledger.fmt(w)} → "
                            f"<b>{ledger.fmt(w + refund)}</b> $")
                else:
                    msg += "\nБаланс не менялся — просто уберу запись."
                context.user_data["pending_delete_expense"] = True
                await update.message.reply_text(msg + "\n\nУдаляем? Ответь «да».",
                                                parse_mode="HTML")
                return

            if money.get("kind") == "expense" and money.get("expense"):
                rec = money["expense"]
                rec.setdefault("date", None)
                rec["date"] = rec.get("date") or _today()
                deduct = bool(money.get("deduct"))
                # Сходимость и курс проверяет Python. Не сошлось — не пишем и спрашиваем:
                # неверная запись в расходах хуже отсутствующей.
                problems = ledger.check_expense(rec) + ledger.check_deduct(rec, deduct)
                if problems:
                    await update.message.reply_text(
                        "❓ Не сходится, поэтому не записываю:\n"
                        + "\n".join(f"• {p}" for p in problems)
                    )
                    return
                context.user_data["pending_expense"] = {"rec": rec, "deduct": deduct}
                await update.message.reply_text(
                    ledger.format_expense_preview(book, rec, deduct)
                    + "\n\nЗаписываем? Ответь «да».",
                    parse_mode="HTML",
                )
                return

            changes = ledger.to_changes(book, money)
            if not changes:
                await update.message.reply_text("В реестре ничего не меняется — суммы те же.")
                return
            summary = money.get("summary") or "Изменение реестра"
            context.user_data["pending_ledger"] = changes
            # Что и почему — нужно журналу в момент подтверждения, а не сейчас.
            context.user_data["pending_ledger_meta"] = {
                "summary": summary,
                "correction": bool(money.get("correction")),
            }
            await update.message.reply_text(
                ledger.format_preview(book, changes, summary, _today(),
                                      correction=bool(money.get("correction"))),
                parse_mode="HTML",
            )
            return

    # Обычная заметка. Если в сообщении была дата — пишем в тот день, иначе в сегодня.
    note_day = None
    date = (route.get("date") or "").strip()
    if _valid_date(date) and date != _today():
        note_day = date

    try:
        structured = structure_note(transcript)
    except Exception as e:
        log.exception("structure error")
        await update.message.reply_text(f"Не смог обработать заметку: {e}")
        return

    append_note(user_id, transcript, structured, day=note_day)
    prefix = f"📅 Записал за {_fmt_date(note_day)}:\n\n" if note_day else ""
    await update.message.reply_text(prefix + structured, reply_markup=MAIN_KEYBOARD)


async def _post_init(app: Application) -> None:
    """Регистрирует список команд — он показывается по кнопке «Menu» и по «/»."""
    await app.bot.set_my_commands(
        [
            BotCommand("balance", "📊 Реестр: балансы и итоги"),
            BotCommand("expenses", "🧾 Расходы: что потрачено"),
            BotCommand("journal", "📔 Журнал: что менялось в реестре"),
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
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("expenses", expenses_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("day", day_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("journal", journal_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    # Inline-кнопки: выбор дня, просмотр/правка/удаление записи.
    app.add_handler(CallbackQueryHandler(on_callback))
    # Нажатия кнопок клавиатуры — до общего обработчика заметок.
    app.add_handler(MessageHandler(filters.Text(ALL_BUTTONS), buttons))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_note))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_note))
    log.info("Бот запущен (модель: %s).", MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()

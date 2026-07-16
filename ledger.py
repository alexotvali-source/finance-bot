"""
Реестр: кто чем владеет и сколько где лежит.

Главный принцип: модель только ИНТЕРПРЕТИРУЕТ то, что сказал Илья, а все итоги
считает этот модуль — детерминированно. Именно на классификации и итогах модель
уже ошибалась: 94 000 положила не в ту корзину, а дебиторку включила
в «под управлением» (завысив на 387 351).

Владение: у Ильи с Дмитрием всё общее и делить не надо, поэтому измерения «чьё»
в схеме нет. «Рабочие средства» — их общие свободные деньги в кошельке.
"""

from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger(__name__)

# Валюта одна — доллары. Рублёвые расходы осознанно вне реестра.
CURRENCY = "$"

EMPTY_LEDGER: dict = {
    "updated_at": None,
    # Операционный кошелёк: деньги физически у нас, но часть — чужая.
    "wallet": {
        "working": 0,  # наши с Дмитрием свободные деньги
        # Чужие деньги без задания: временно лежат у нас, пока не появится задание.
        # Это НЕ инвесторы — доходность не обещана, это хранение.
        "held": {},    # имя -> сумма
    },
    "assets": {},       # наши активы вне кошелька: название -> сумма
    "receivables": {},  # дебиторка (нам должны), тоже общая: имя -> сумма
    # Журнал расходов. Рубли тут — ОПИСАНИЕ (как было сказано), по балансу бьют
    # только доллары, списанные с рабочих средств. Поэтому баланс остаётся
    # долларовым и не дрейфует от курса.
    "expenses": [],
}


def _num(x) -> float:
    # Старый формат позиции — объект {"amount": N, "verified": ...}. Терпим его,
    # иначе уже сохранённый реестр читается как сплошные нули.
    if isinstance(x, dict):
        x = x.get("amount")
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------- Итоги (единственный источник арифметики) ----------
def compute(ledger: dict) -> dict:
    """Считает все производные суммы. Модель к этому не допускается."""
    wallet = ledger.get("wallet") or {}
    working = _num(wallet.get("working"))
    held = wallet.get("held") or {}
    held_total = sum(_num(v) for v in held.values())
    wallet_total = working + held_total

    assets = ledger.get("assets") or {}
    assets_total = sum(_num(v) for v in assets.values())

    receivables = ledger.get("receivables") or {}
    receivables_total = sum(_num(v) for v in receivables.values())

    return {
        "working": working,            # рабочий баланс: свободные деньги Ильи и Дмитрия
        "held_total": held_total,      # в управлении: чужие деньги, лежат у нас
        "wallet_total": wallet_total,  # весь операционный кошелёк
        "assets_total": assets_total,  # наши активы вне кошелька
        "receivables_total": receivables_total,
        # Наше (Ильи + Дмитрия). Дебиторка входит — наши деньги, просто у других.
        # Чужие деньги в управлении НЕ входят: они не наши.
        "our_assets": working + assets_total + receivables_total,
    }


# ---------- Позиции: адресация путём "assets.Наличка" ----------
def paths(ledger: dict) -> list[str]:
    """Все существующие позиции. Скармливаем модели, чтобы она не выдумывала корзины."""
    out = ["wallet.working"]
    out += [f"wallet.held.{n}" for n in (ledger.get("wallet", {}).get("held") or {})]
    out += [f"assets.{n}" for n in (ledger.get("assets") or {})]
    out += [f"receivables.{n}" for n in (ledger.get("receivables") or {})]
    return out


def get(ledger: dict, path: str) -> float:
    """Сумма позиции. Несуществующая позиция — это 0 (новый человек, новая корзина)."""
    parts = path.split(".")
    if parts == ["wallet", "working"]:
        return _num(ledger.get("wallet", {}).get("working"))
    if len(parts) == 3 and parts[0] == "wallet" and parts[1] == "held":
        return _num((ledger.get("wallet", {}).get("held") or {}).get(parts[2]))
    if len(parts) == 2 and parts[0] in ("assets", "receivables"):
        return _num((ledger.get(parts[0]) or {}).get(parts[1]))
    raise ValueError(f"неизвестный путь: {path}")


def _set(ledger: dict, path: str, value: float) -> None:
    parts = path.split(".")
    if parts == ["wallet", "working"]:
        ledger.setdefault("wallet", {})["working"] = value
    elif len(parts) == 3 and parts[0] == "wallet" and parts[1] == "held":
        ledger.setdefault("wallet", {}).setdefault("held", {})[parts[2]] = value
    elif len(parts) == 2 and parts[0] in ("assets", "receivables"):
        ledger.setdefault(parts[0], {})[parts[1]] = value
    else:
        raise ValueError(f"неизвестный путь: {path}")


def label(path: str) -> str:
    """Человеческое имя позиции."""
    if path == "wallet.working":
        return "Рабочий баланс"
    parts = path.split(".")
    if len(parts) == 3:
        return f"{parts[2]} (в управлении)"
    if parts[0] == "receivables":
        return f"{parts[1]} (долг нам)"
    return parts[1]


# Виртуальный путь: не позиция, а весь кошелёк целиком (рабочий + в управлении).
# Илья называет именно эту цифру — «операционный баланс».
OPERATIONAL = "wallet.operational"


def to_changes(ledger: dict, ops: dict) -> list:
    """Превращает названные Ильёй остатки ("set") и изменения ("add") в дельты.

    Вычитание делает Python, а не модель: модель только называет сумму, которую
    услышала. Именно на арифметике и классификации она уже ошибалась.
    """
    changes = []
    operational = None
    for item in ops.get("set") or []:
        path = item["path"]
        amount = _num(item.get("amount"))
        if path == OPERATIONAL:
            # Считаем последним: см. ниже.
            operational = amount
            continue
        changes.append({"path": path, "amount": amount - get(ledger, path)})
    for item in ops.get("add") or []:
        path = item["path"]
        if path == OPERATIONAL:
            # «Операционный вырос на 100 000» — чьи это деньги, наши или чужие?
            # Не угадываем: разница между прибылью и чужим взносом здесь и живёт.
            raise ValueError(
                "сказано про изменение операционного баланса, но не сказано, чьи это деньги. "
                "Назови новый остаток целиком или укажи позицию (рабочий баланс / имя)"
            )
        changes.append({"path": path, "amount": _num(item.get("amount"))})

    if operational is not None:
        # Наш только остаток после вычета чужого:
        #   рабочий = операционный − в управлении
        # Иначе чужие деньги (у Макса и НИКО их 1,4 млн) станут «прибылью» Ильи.
        # Считаем ПОСЛЕ остальных изменений: если в том же сообщении назван и новый
        # баланс Макса, операционный назван уже с его учётом — иначе задвоим.
        after = apply(ledger, changes, ledger.get("updated_at") or "")
        changes.append(
            {"path": "wallet.working", "amount": operational - compute(after)["wallet_total"]}
        )

    # Пустые изменения выкидываем: «Стефан 325 168», когда там уже 325 168, — не событие.
    return [c for c in changes if round(c["amount"]) != 0]


def apply(ledger: dict, changes: list, today: str) -> dict:
    """Применяет изменения, возвращая НОВЫЙ реестр. Исходный не трогает."""
    new = json.loads(json.dumps(ledger))
    for c in changes:
        path = c["path"]
        _set(new, path, get(new, path) + _num(c.get("amount")))
    # Позиции, ушедшие в ноль, убираем — чтобы реестр не зарастал нулями.
    for bucket in ("assets", "receivables"):
        for name in [k for k, v in (new.get(bucket) or {}).items() if _num(v) == 0]:
            del new[bucket][name]
    held = new.get("wallet", {}).get("held") or {}
    for name in [k for k, v in held.items() if _num(v) == 0]:
        del held[name]
    new["updated_at"] = today
    return new


def net_external(changes: list) -> float:
    """Сколько денег втекает извне (+) или утекает наружу (-).

    Если сумма изменений не ноль — деньги пересекли границу системы. Само по себе
    это законно (приход, оплата), но должно быть НАЗВАНО вслух: именно так молча
    исчезли 1 258 196 у Макса.
    """
    return sum(_num(c.get("amount")) for c in changes)


def log_entries(before: dict, after: dict, changes: list, summary: str,
                correction: bool, at: str) -> list:
    """Строки журнала: что именно изменилось, когда и почему.

    Живут строками на отдельном листе, а НЕ внутри JSON реестра: канонический JSON
    лежит в одной ячейке _data!A1 с лимитом 50 000 символов, и растущий журнал рано
    или поздно упёрся бы в него — то есть уронил бы запись самого реестра.
    """
    ours = compute(after)["our_assets"]
    return [
        {
            "at": at,
            "kind": "correction" if correction else "operation",
            "path": c["path"],
            "label": label(c["path"]),
            "before": get(before, c["path"]),
            "after": get(after, c["path"]),
            "amount": _num(c.get("amount")),
            # Наши активы на момент операции — чтобы журнал читался сам по себе,
            # без пересчёта всей истории.
            "our_assets": ours,
            "summary": summary,
        }
        for c in changes
    ]


def read_journal(notes_dir: str, user_id: str, limit: int = 10) -> list:
    """Последние строки журнала из того же источника, где он лежит."""
    import sheet

    if sheet.enabled():
        return sheet.journal(limit)
    p = os.path.join(os.path.dirname(_path(notes_dir, user_id)), "ledger_log.jsonl")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    out = []
    for r in rows[-limit:]:
        at = str(r.get("at") or "")
        date, _, time = at.partition(" ")
        out.append({
            "date": date, "time": time,
            "kind": "правка данных" if r.get("kind") == "correction" else "операция",
            "label": r.get("label") or r.get("path") or "",
            "before": _num(r.get("before")), "after": _num(r.get("after")),
            "amount": _num(r.get("amount")), "our_assets": _num(r.get("our_assets")),
            "summary": r.get("summary") or "",
        })
    return out


def _journal_when(r: dict, fmt_date) -> str:
    """«16-07-26 15:19» из чего угодно, что лежит в ячейках даты и времени.

    Ранние строки журнала Sheets хранил датами, и наружу они могли выйти
    как «2026-07-16T00:00:00.000Z» — парсер такого не ждал и молча
    показывал ISO как есть. Нормализуем вход, а не надеемся на чистоту."""
    date = str(r.get("date") or "").strip()[:10]
    time = str(r.get("time") or "").strip()
    if "T" in time and len(time) >= 16:  # «1899-12-30T15:19:00.000Z» -> «15:19»
        time = time[11:16]
    return f"{fmt_date(date)} {time}".strip()


def format_journal(rows: list, fmt_date=lambda s: s) -> str:
    """Журнал для телефона: хронологически, старое сверху, две строки на запись."""
    if not rows:
        return ("📔 <b>Журнал пуст</b>\n\nЗдесь будет каждое изменение реестра. "
                "Записи появятся начиная с первой операции.")
    lines = [f"📔 <b>Журнал — последние {len(rows)}</b>\n"]
    for r in rows:
        d = _journal_when(r, fmt_date)
        mark = "🛠" if r["kind"].startswith("правка") else "•"
        amount = _num(r["amount"])
        sign = "+" if amount > 0 else "−"
        # В ранние описания дата вклеена текстом «(2026-07-16)» — здесь она мусор,
        # рядом есть нормальная. Историю в листе не трогаем, чистим только показ.
        summary = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)", "", r.get("summary") or "").strip()
        head = f"{mark} <b>{d}</b>" + (f" · {summary}" if summary else "")
        lines.append(head)
        lines.append(f"   {r['label']}: {sign}{fmt(abs(amount))} → "
                     f"<b>{fmt(r['after'])}</b> {CURRENCY}")
    return "\n".join(lines)


def format_preview(before: dict, changes: list, summary: str, today: str,
                   correction: bool = False) -> str:
    """Что именно изменится. Показываем до применения — тут ловятся ошибки.

    correction=True — Илья правит неверно записанную цифру. Движения денег не было,
    поэтому называть разницу приростом или расходом нельзя: это ложь про деньги.
    """
    after = apply(before, changes, today)
    tb, ta = compute(before), compute(after)
    lines = [("🛠 <b>Правка данных</b>\n" if correction else "") + f"📝 <b>{summary}</b>\n"]

    for c in changes:
        p = c["path"]
        lines.append(f"• {label(p)}: {fmt(get(before, p))} → <b>{fmt(get(after, p))}</b> {CURRENCY}")

    # Внешний поток называем вслух, но НЕ требуем объяснений: рост рабочих средств —
    # это прибыль, её источник Илья ведёт отдельно. Задача превью — показать, а не допросить.
    net = net_external(changes)
    if correction:
        if round(net) != 0:
            lines.append(f"\n🛠 <b>Исправление цифры на {fmt(abs(net))} {CURRENCY}</b> — "
                         "движения денег не было, это не прибыль и не расход.")
    elif net > 0:
        lines.append(f"\n⬅️ <b>Прирост: +{fmt(net)} {CURRENCY}</b>")
    elif net < 0:
        lines.append(f"\n➡️ <b>Ушло наружу: {fmt(-net)} {CURRENCY}</b>")

    for name, key in (
        ("Рабочий баланс", "working"),
        ("В управлении", "held_total"),
        ("Наши активы", "our_assets"),
    ):
        if round(tb[key]) != round(ta[key]):
            d = ta[key] - tb[key]
            sign = "+" if d > 0 else "−"
            lines.append(f"\n{name}: {fmt(tb[key])} → <b>{fmt(ta[key])}</b>  ({sign}{fmt(abs(d))})")

    lines.append("\nПрименяем? Ответь «да».")
    return "\n".join(lines)


# ---------- Форматирование ----------
def fmt(x) -> str:
    return f"{_num(x):,.0f}".replace(",", " ")


def format_balance(ledger: dict, fmt_date=lambda s: s) -> str:
    t = compute(ledger)
    wallet = ledger.get("wallet") or {}
    lines = ["📊 <b>Реестр</b>"]

    # Рабочий баланс — сюда приходит прибыль и отсюда уходят расходы.
    lines.append(f"\n💼 <b>Рабочий баланс: {fmt(t['working'])} {CURRENCY}</b>")

    held = wallet.get("held") or {}
    if held:
        lines.append(
            f"\n🤝 <b>В управлении — {fmt(t['held_total'])} {CURRENCY}</b> "
            f"<i>(чужие деньги, лежат у нас)</i>"
        )
        for name, amount in held.items():
            lines.append(f"• {name}: {fmt(amount)} {CURRENCY}")

    assets = ledger.get("assets") or {}
    if assets:
        lines.append(f"\n💰 <b>Активы — {fmt(t['assets_total'])} {CURRENCY}</b>")
        for name, amount in assets.items():
            lines.append(f"• {name}: {fmt(amount)} {CURRENCY}")

    recv = ledger.get("receivables") or {}
    if recv:
        lines.append(f"\n📌 <b>Дебиторка — {fmt(t['receivables_total'])} {CURRENCY}</b>")
        for name, amount in recv.items():
            lines.append(f"• {name}: {fmt(amount)} {CURRENCY}")

    lines.append(
        f"\n<b>Наши активы (Илья + Дмитрий): {fmt(t['our_assets'])} {CURRENCY}</b>"
        f"\n<i>рабочий баланс + активы + дебиторка; чужие деньги не входят</i>"
    )
    if ledger.get("updated_at"):
        lines.append(f"<i>обновлено: {fmt_date(ledger['updated_at'])}</i>")
    return "\n".join(lines)


# ---------- Расходы ----------
def check_expense(rec: dict) -> list[str]:
    """Проверяет запись расхода на сходимость. Считает Python, а не модель."""
    problems = []
    by_person = rec.get("by_person") or {}
    # Валюты проверяем раздельно: запись может быть только рублёвой или только
    # долларовой. Проверяем ту, по которой итог вообще заявлен.
    for cur, key, sign in (("rub", "total_rub", "₽"), ("usd", "total_usd", "$")):
        total = _num(rec.get(key))
        parts_sum = sum(_num(v.get(cur)) for v in by_person.values())
        # Сверяем ТОЛЬКО когда разбивка вообще дана. «Общий расход 15 000» без имён —
        # это законная запись, а не расхождение: сверять итог не с чем.
        if parts_sum and round(parts_sum) != round(total):
            problems.append(
                f"разбивка по людям даёт {fmt(parts_sum)} {sign}, "
                f"а итого указано {fmt(total)} {sign}"
            )
    total = _num(rec.get("total_rub"))
    covered = _num(rec.get("covered_by_profit_rub"))
    paid_rub = _num((rec.get("paid_from_working") or {}).get("rub"))
    # Источник проверяем, только если про него вообще сказано: в записи может
    # стоять просто «потрачено столько-то», без указания, откуда деньги.
    if (covered or paid_rub) and round(covered + paid_rub) != round(total):
        problems.append(
            f"покрыто прибылью {fmt(covered)} ₽ + оплачено с общих {fmt(paid_rub)} ₽ "
            f"= {fmt(covered + paid_rub)} ₽, а итого {fmt(total)} ₽"
        )
    return problems


def paid_usd(rec: dict) -> float:
    """Сколько долларов ушло с рабочего баланса по этой записи."""
    return _num((rec.get("paid_from_working") or {}).get("usd"))


def deduct_usd(rec: dict) -> float:
    """Сколько долларов списать с баланса.

    Приоритет — явно названному paid_from_working.usd (там курс уже посчитан Ильёй).
    Если его нет, но расход ЦЕЛИКОМ долларовый — списываем его итог: доллары уже
    названы, курс придумывать не надо. Рублёвый расход без долларов сюда не попадёт.
    """
    p = paid_usd(rec)
    if p:
        return p
    if _num(rec.get("total_usd")) and not _num(rec.get("total_rub")):
        return _num(rec.get("total_usd"))
    return 0.0


def check_deduct(rec: dict, deduct: bool) -> list[str]:
    """Можно ли списать этот расход с рабочего баланса.

    Реестр долларовый, а расходы Илья называет чаще в рублях. Курс НЕ выдумываем:
    ни модель, ни программа не знают, по какому курсу он менял. Нет долларовой
    суммы — списывать нечего, надо спросить.
    """
    if not deduct:
        return []
    if not deduct_usd(rec):
        return ["сколько это в долларах? Реестр долларовый, а курс я не придумываю"]
    return []


def format_expense_preview(before: dict, rec: dict, deduct: bool) -> str:
    """Что запишем в расходы и тронем ли баланс. Показываем до применения."""
    lines = ["🧾 <b>Расход</b>\n"]
    if rec.get("note"):
        lines.append(f"• {rec['note']}")
    if rec.get("period"):
        lines.append(f"• Период: {rec['period']}")
    by_person = rec.get("by_person") or {}
    for name, v in by_person.items():
        parts = []
        if _num(v.get("rub")):
            parts.append(f"{fmt(v['rub'])} ₽")
        if _num(v.get("usd")):
            parts.append(f"{fmt(v['usd'])} $")
        lines.append(f"• {name}: {' / '.join(parts)}")
    if not by_person:
        # Говорим об этом ДО «да»: безымянный расход не прибавится никому
        # в накопительных итогах — молча записать такое нельзя.
        lines.append("• Кто: <b>не указано</b> — в итоги по людям не попадёт")
    totals = []
    if _num(rec.get("total_rub")):
        totals.append(f"{fmt(rec['total_rub'])} ₽")
    if _num(rec.get("total_usd")):
        totals.append(f"{fmt(rec['total_usd'])} $")
    if totals:
        lines.append(f"\n<b>Итого: {' / '.join(totals)}</b>")

    usd = deduct_usd(rec)
    if deduct and usd:
        w = get(before, "wallet.working")
        lines.append(f"\n➡️ <b>Списываю с рабочего баланса {fmt(usd)} {CURRENCY}</b>")
        lines.append(f"Рабочий баланс: {fmt(w)} → <b>{fmt(w - usd)}</b> {CURRENCY}")
    else:
        # Молчать об этом нельзя: иначе Илья решит, что баланс уменьшился, а он нет.
        lines.append("\n📌 <b>Баланс не трогаю</b> — только записываю в расходы. "
                     "Скажешь новый операционный баланс — я его и возьму.")
    return "\n".join(lines)


def expense_rate(rec: dict) -> float | None:
    """Курс, зашитый в саму запись. К балансу не применяется — только справка."""
    p = rec.get("paid_from_working") or {}
    rub, usd = _num(p.get("rub")), _num(p.get("usd"))
    return rub / usd if usd else None


def add_expense(ledger: dict, rec: dict, deduct: bool, today: str) -> dict:
    """Кладёт расход в журнал. deduct=False — если доллары уже списаны раньше.

    По умолчанию баланс НЕ трогаем: Илья называет балансы снимками, и снимок
    затрёт списание — расход учёлся бы дважды или потерялся. Списываем, только
    если он прямо сказал. Поэтому в самой записи помечаем, тронут баланс или нет:
    через месяц по сумме этого уже не понять.
    """
    new = json.loads(json.dumps(ledger))
    rec = json.loads(json.dumps(rec))
    rec["deducted"] = bool(deduct)
    # Момент добавления — для «удали последний расход»: записи сортируются по дате
    # траты, и без этой метки последней считалась бы не та, что добавлена только что.
    rec["added_at"] = f"{today} #{len(new.get('expenses') or []):03d}"
    usd = deduct_usd(rec)
    if deduct and usd:
        # Фиксируем списанную сумму в самой записи, даже если Илья назвал только
        # долларовый итог: иначе в «Расходах» не будет видно, что и сколько списано.
        rec.setdefault("paid_from_working", {})["usd"] = usd
    new.setdefault("expenses", []).append(rec)
    new["expenses"].sort(key=lambda r: r.get("date") or "")
    if deduct:
        _set(new, "wallet.working", get(new, "wallet.working") - usd)
    new["updated_at"] = today
    return new


def remove_last_expense(ledger: dict, today: str):
    """Отменяет последний ДОБАВЛЕННЫЙ расход (не последний по дате траты: отменяют
    обычно то, что только что записали). Списанное возвращается на рабочий баланс.

    Возвращает (новый реестр, удалённая запись, возвращённые доллары) или None.
    """
    new = json.loads(json.dumps(ledger))
    recs = new.get("expenses") or []
    if not recs:
        return None
    # У старых записей added_at нет — среди них последней считаем нижнюю по списку.
    i = max(range(len(recs)), key=lambda k: (recs[k].get("added_at") or "", k))
    rec = recs.pop(i)
    refund = deduct_usd(rec) if rec.get("deducted") else 0.0
    if refund:
        _set(new, "wallet.working", get(new, "wallet.working") + refund)
    new["updated_at"] = today
    return new, rec, refund


def describe_expense(rec: dict, with_date: bool = True) -> str:
    """«купил Старкнет — Илья: 10 000 $ (16-07-26)» — чтобы было ясно, ЧТО удаляем.

    with_date=False — для журнала: там дата стоит отдельной колонкой,
    и вклеенная в текст она просто мусор."""
    who = ", ".join(f"{n}: {_money(v.get('rub'), v.get('usd'))}"
                    for n, v in (rec.get("by_person") or {}).items())
    total = _money(rec.get("total_rub"), rec.get("total_usd"))
    bits = [b for b in (rec.get("note"), who or f"итого {total}") if b]
    out = " — ".join(bits)
    if with_date and rec.get("date"):
        out += f" ({rec['date']})"
    return out


def expense_totals(ledger: dict) -> dict:
    """Накопительные итоги по журналу."""
    rub_by_person: dict = {}
    usd_by_person: dict = {}
    total_rub = total_usd = paid_usd = covered_rub = 0.0
    for rec in ledger.get("expenses") or []:
        for name, v in (rec.get("by_person") or {}).items():
            rub_by_person[name] = rub_by_person.get(name, 0) + _num(v.get("rub"))
            usd_by_person[name] = usd_by_person.get(name, 0) + _num(v.get("usd"))
        total_rub += _num(rec.get("total_rub"))
        total_usd += _num(rec.get("total_usd"))
        covered_rub += _num(rec.get("covered_by_profit_rub"))
        paid_usd += _num((rec.get("paid_from_working") or {}).get("usd"))
    return {
        "rub_by_person": rub_by_person,
        "usd_by_person": usd_by_person,
        "total_rub": total_rub,
        "total_usd": total_usd,      # потрачено в долларах (расход, а не списание с общих)
        "covered_rub": covered_rub,
        "paid_usd": paid_usd,        # сколько ушло именно с рабочих средств
    }


def _plural(n: int, one: str, few: str, many: str) -> str:
    """1 запись / 2 записи / 5 записей."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _money(rub, usd) -> str:
    """«2 951 930 ₽ + 401 847 $». Валюты не складываем: курс у каждой записи свой."""
    bits = []
    if _num(rub):
        bits.append(f"{fmt(rub)} ₽")
    if _num(usd):
        bits.append(f"{fmt(usd)} $")
    return " + ".join(bits) or "0"


def format_expenses(ledger: dict, fmt_date=lambda s: s) -> str:
    """Сводка для кнопки: сколько всего и по кому. Полная расшифровка по каждой
    записи — в таблице, лист «Расходы»; дублировать её в бот незачем."""
    recs = ledger.get("expenses") or []
    if not recs:
        return ("🧾 <b>Расходов пока нет</b>\n\n"
                "Скажи «потратил 300 000 на офис» — запишу сюда.")
    t = expense_totals(ledger)
    lines = [f"🧾 <b>Всего потрачено: {_money(t['total_rub'], t['total_usd'])}</b>\n"]
    for name in t["rub_by_person"]:
        lines.append(f"• {name}: "
                     f"{_money(t['rub_by_person'].get(name), t['usd_by_person'].get(name))}")
    # «С рабочего баланса ушло» здесь НЕ показываем — Илья попросил убрать:
    # эта цифра и так видна в таблице, а кнопка — короткая сводка.
    word = _plural(len(recs), "запись", "записи", "записей")
    lines.append(f"\n<i>Всё по датам — в таблице, лист «Расходы» ({len(recs)} {word}).</i>")
    return "\n".join(lines)


# ---------- Хранение ----------
def _path(notes_dir: str, user_id: str) -> str:
    d = os.path.join(notes_dir, str(user_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "ledger.json")


def migrate(book: dict) -> dict:
    """Приводит реестр к текущей схеме. Нужна, потому что схема менялась поверх
    уже сохранённого файла: позиция была {"amount": N, "verified": ...}, а корзина
    чужих денег называлась "investors". Без этого старый файл читается как нули."""
    wallet = book.get("wallet") or {}
    wallet["working"] = _num(wallet.get("working"))
    held = {k: _num(v) for k, v in (wallet.get("held") or {}).items()}
    for name, v in (wallet.pop("investors", None) or {}).items():  # старое имя корзины
        held[name] = _num(v)
    wallet["held"] = held
    book["wallet"] = wallet
    for bucket in ("assets", "receivables"):
        book[bucket] = {k: _num(v) for k, v in (book.get(bucket) or {}).items()}
    # Доносим стартовые записи, которых ещё нет — сверяем по дате, чтобы правка
    # доезжала и в уже непустой журнал (иначе новая стартовая запись не попадёт
    # в файл, созданный прошлым деплоем). Удалить расход из бота сейчас нельзя —
    # команды нет, — поэтому воскрешать нечего. Когда удаление появится,
    # это правило придётся пересмотреть.
    book.setdefault("expenses", [])
    have = {r.get("date") for r in book["expenses"]}
    for r in SEED["expenses"]:
        if r.get("date") not in have:
            book["expenses"].append(json.loads(json.dumps(r)))
    book["expenses"].sort(key=lambda r: r.get("date") or "")
    return book


def load(notes_dir: str, user_id: str) -> dict:
    p = _path(notes_dir, user_id)
    if not os.path.exists(p):
        return json.loads(json.dumps(EMPTY_LEDGER))
    with open(p, encoding="utf-8") as f:
        return migrate(json.load(f))


def save(notes_dir: str, user_id: str, ledger: dict) -> None:
    with open(_path(notes_dir, user_id), "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)


def read(notes_dir: str, user_id: str) -> dict:
    """Читает реестр из единственного источника правды.

    Настроена таблица — правда в ней; при её сбое поднимаем ошибку, а НЕ
    подставляем локальную копию: тихо устаревшая цифра опаснее честной ошибки.
    Таблица не настроена — работаем на волюме, как раньше.
    """
    import sheet

    if not sheet.enabled():
        return load_or_seed(notes_dir, user_id)

    book = sheet.load()  # SheetError уйдёт наверх — так и надо
    if book is None:  # таблица пустая: первый запуск
        book = json.loads(json.dumps(SEED))
        sheet.save(book, backup_path=_path(notes_dir, user_id))
        return book
    book = migrate(book)
    return book


def write(notes_dir: str, user_id: str, book: dict, entries: list | None = None) -> None:
    """Пишет реестр в единственный источник правды. entries — строки журнала."""
    import sheet

    if not sheet.enabled():
        save(notes_dir, user_id, book)
        _append_local_log(notes_dir, user_id, entries)
        return
    sheet.save(book, entries=entries, backup_path=_path(notes_dir, user_id))


def _append_local_log(notes_dir: str, user_id: str, entries: list | None) -> None:
    """Журнал без таблицы — в JSONL рядом с реестром. Дописываем, не переписываем:
    журнал только растёт, иначе он не журнал."""
    if not entries:
        return
    p = os.path.join(os.path.dirname(_path(notes_dir, user_id)), "ledger_log.jsonl")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except Exception:
        # Реестр уже сохранён — терять его из-за журнала нельзя.
        log.exception("не смог дописать локальный журнал")


def load_or_seed(notes_dir: str, user_id: str) -> dict:
    """Первый запуск — кладём стартовые цифры. Дальше читаем и, если файл в старой
    схеме, лечим его на месте — чтобы миграция прошла один раз, а не при каждом чтении."""
    if os.path.exists(_path(notes_dir, user_id)):
        book = load(notes_dir, user_id)
        # Пустой реестр из старой схемы (все нули) — значит миграция потеряла данные
        # или файл создался до заполнения. Заполняем стартовыми цифрами.
        if compute(book)["wallet_total"] == 0 and not book.get("expenses"):
            book = json.loads(json.dumps(SEED))
        save(notes_dir, user_id, book)
        return book
    seeded = json.loads(json.dumps(SEED))
    save(notes_dir, user_id, seeded)
    return seeded


# ---------- Стартовое состояние: расклад Ильи на 14.07.2026 ----------
SEED = {
    "updated_at": "2026-07-14",
    "wallet": {
        "working": 376_698,
        "held": {
            "Стефан": 325_168,
            "Вадим": 80_000,
            "Хайдар": 8_116,
            "Макс": 161_950,
            "Дмитрий Великий": 137_098,
        },
    },
    "assets": {
        "Крипта": 1_069_000,
        "Наличка": 105_750,
        "Заморожено (счёт компании)": 82_214,
    },
    "receivables": {
        "Фэйгэ": 300_000,
        "Лэлэ": 27_200,
        "Зама": 20_000,
        "Степан": 20_151,
        "Хасан": 20_000,
    },
    # Операция 13.07 уже отражена в рабочих средствах (376 698 — это ПОСЛЕ неё),
    # поэтому лежит здесь как история: повторно списывать нельзя.
    "expenses": [
        {
            "date": "2026-03-14",
            "period": "до 15.03",
            "note": "Личные расходы Ильи до начала периода",
            "by_person": {"Илья": {"rub": 2_736_000}},
            "total_rub": 2_736_000,
            # Про источник (прибыль или общие) Илья не говорил — не выдумываем.
        },
        {
            "date": "2026-07-12",
            "period": "до 14.07",
            "note": "Личные расходы в долларах за длительный период",
            "by_person": {
                "Илья": {"usd": 401_847},
                "Дмитрий": {"usd": 250_635},
            },
            "total_usd": 652_482,
            # Уже отражены в рабочих средствах (376 698 — это ПОСЛЕ них).
            # Источник Илья не называл — не выдумываем.
        },
        {
            "date": "2026-07-13",
            "period": "15.03–13.07",
            "note": "Личные расходы Ильи и Дмитрия + общие",
            "by_person": {
                "Илья": {"rub": 2_951_930},
                "Дмитрий": {"rub": 10_527_758},  # 5 986 658 + 4 541 100
                "Общие": {"rub": 2_793_500},
            },
            "total_rub": 16_273_188,
            "covered_by_profit_rub": 7_257_658,  # прибыль с других проектов, в кошелёк не заходила
            "paid_from_working": {"rub": 9_015_530, "usd": 112_835},  # курс 79,90
        }
    ],
}

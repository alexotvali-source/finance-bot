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
import os

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
}


def _num(x) -> float:
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
        "working": working,            # наши свободные деньги в кошельке
        "held_total": held_total,      # чужие деньги без задания, лежат у нас
        "wallet_total": wallet_total,  # весь кошелёк
        "assets_total": assets_total,  # наши активы вне кошелька
        "receivables_total": receivables_total,
        # Всё, что физически под нашим контролем. Дебиторка НЕ входит:
        # этих денег у нас нет, распорядиться ими нельзя.
        "under_management": wallet_total + assets_total,
        # Наше (Ильи + Дмитрия). Дебиторка входит — наши деньги, просто у других.
        "our_assets": working + assets_total + receivables_total,
    }


# ---------- Форматирование ----------
def fmt(x) -> str:
    return f"{_num(x):,.0f}".replace(",", " ")


def format_balance(ledger: dict, fmt_date=lambda s: s) -> str:
    t = compute(ledger)
    wallet = ledger.get("wallet") or {}
    lines = ["📊 <b>Реестр</b>"]

    lines.append(f"\n💼 <b>Операционный кошелёк</b> — {fmt(t['wallet_total'])} {CURRENCY}")
    lines.append(f"• Рабочие средства: {fmt(t['working'])} {CURRENCY}")

    held = wallet.get("held") or {}
    if held:
        lines.append(f"\n  <i>Без задания — лежат у нас, {fmt(t['held_total'])} {CURRENCY}</i>")
        for name, amount in held.items():
            lines.append(f"• {name}: {fmt(amount)} {CURRENCY}")

    assets = ledger.get("assets") or {}
    if assets:
        lines.append(f"\n💰 <b>Активы вне кошелька</b> — {fmt(t['assets_total'])} {CURRENCY}")
        for name, amount in assets.items():
            lines.append(f"• {name}: {fmt(amount)} {CURRENCY}")

    recv = ledger.get("receivables") or {}
    if recv:
        lines.append(f"\n🤝 <b>Дебиторка</b> — {fmt(t['receivables_total'])} {CURRENCY}")
        for name, amount in recv.items():
            lines.append(f"• {name}: {fmt(amount)} {CURRENCY}")

    lines.append(
        f"\n<b>Наши активы (Илья + Дмитрий): {fmt(t['our_assets'])} {CURRENCY}</b>"
        f"\n<b>Под управлением: {fmt(t['under_management'])} {CURRENCY}</b>"
        f"\n<i>под управлением = кошелёк + активы, без дебиторки</i>"
    )
    if ledger.get("updated_at"):
        lines.append(f"<i>обновлено: {fmt_date(ledger['updated_at'])}</i>")
    return "\n".join(lines)


# ---------- Хранение ----------
def _path(notes_dir: str, user_id: str) -> str:
    d = os.path.join(notes_dir, str(user_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "ledger.json")


def load(notes_dir: str, user_id: str) -> dict:
    p = _path(notes_dir, user_id)
    if not os.path.exists(p):
        return json.loads(json.dumps(EMPTY_LEDGER))
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(notes_dir: str, user_id: str, ledger: dict) -> None:
    with open(_path(notes_dir, user_id), "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)


def load_or_seed(notes_dir: str, user_id: str) -> dict:
    """Первый запуск — кладём стартовые цифры. Дальше просто читаем."""
    if os.path.exists(_path(notes_dir, user_id)):
        return load(notes_dir, user_id)
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
}

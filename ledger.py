"""
Реестр: кто чем владеет и сколько где лежит.

Главный принцип: модель только ИНТЕРПРЕТИРУЕТ то, что ты сказал, а все итоги
считает этот модуль — детерминированно. Именно на арифметике и классификации
модель уже ошибалась (94 000 не в ту корзину, дебиторка в «под управлением»).

Владение: у Ильи с Дмитрием всё общее и делить не надо, поэтому измерения «чьё»
в схеме нет. «Рабочие средства» — их общие свободные деньги.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

# Валюта одна — доллары. Рублёвые расходы осознанно вне реестра.
CURRENCY = "$"

# Пустой реестр — на случай, если хранилище ещё пустое.
EMPTY_LEDGER: dict = {
    "updated_at": None,
    # Операционный кошелёк: деньги физически у нас, но часть — чужая.
    "wallet": {
        "working": {"amount": 0, "verified": None},  # наши с Дмитрием свободные деньги
        # Чужие деньги без задания: временно лежат у нас, пока не появится задание.
        # Это НЕ инвесторы — доходность им не обещана, это хранение.
        "held": {},  # имя -> {amount, verified}
    },
    # Наши активы вне кошелька.
    "assets": {},        # название -> {amount, verified}
    # Дебиторка: нам должны. Тоже общая.
    "receivables": {},   # имя -> {amount, verified}
}


# ---------- Позиция ----------
def position(amount: float, verified: str | None = None) -> dict:
    """Позиция реестра. verified — дата последней сверки с реальностью (или None)."""
    return {"amount": amount, "verified": verified}


def _amount(pos) -> float:
    """Сумма позиции. Терпит как {'amount': N}, так и голое число."""
    if isinstance(pos, dict):
        try:
            return float(pos.get("amount", 0))
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(pos)
    except (TypeError, ValueError):
        return 0.0


def _verified(pos) -> str | None:
    return pos.get("verified") if isinstance(pos, dict) else None


# ---------- Итоги (единственный источник арифметики) ----------
def compute(ledger: dict) -> dict:
    """Считает все производные суммы. Модель к этому не допускается."""
    wallet = ledger.get("wallet") or {}
    working = _amount(wallet.get("working"))
    held = wallet.get("held") or {}
    held_total = sum(_amount(v) for v in held.values())
    wallet_total = working + held_total

    assets = ledger.get("assets") or {}
    assets_total = sum(_amount(v) for v in assets.values())

    receivables = ledger.get("receivables") or {}
    receivables_total = sum(_amount(v) for v in receivables.values())

    return {
        "working": working,               # наши свободные деньги в кошельке
        "held_total": held_total,         # чужие деньги без задания, лежат у нас
        "wallet_total": wallet_total,     # весь кошелёк
        "assets_total": assets_total,     # наши активы вне кошелька
        "receivables_total": receivables_total,
        # Всё, что физически под нашим контролем. Дебиторка НЕ входит:
        # этих денег у нас нет, распорядиться ими нельзя.
        "under_management": wallet_total + assets_total,
        # Наше (Ильи + Дмитрия). Дебиторка входит — это наши деньги, просто у других.
        "our_assets": working + assets_total + receivables_total,
    }


# ---------- Форматирование ----------
def fmt(x) -> str:
    return f"{float(x):,.0f}".replace(",", " ")


def _age(verified: str | None, today: str) -> str:
    """Насколько цифра протухла. Возраст виден прямо у суммы — никто не дёргает."""
    if not verified:
        return "❌ не сверялось"
    try:
        d = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(verified, "%Y-%m-%d")).days
    except ValueError:
        return "❌ не сверялось"
    if d == 0:
        return "✅ сверено сегодня"
    if d <= 30:
        return f"✅ сверено {d} дн. назад"
    return f"⚠️ сверено {d} дн. назад"


def format_balance(ledger: dict, today: str, fmt_date=lambda s: s) -> str:
    t = compute(ledger)
    wallet = ledger.get("wallet") or {}
    lines = ["📊 <b>Реестр</b>"]

    lines.append(f"\n💼 <b>Операционный кошелёк</b> — {fmt(t['wallet_total'])} {CURRENCY}")
    lines.append(
        f"• Рабочие средства: {fmt(t['working'])} {CURRENCY}  "
        f"{_age(_verified(wallet.get('working')), today)}"
    )
    held = wallet.get("held") or {}
    if held:
        lines.append(f"\n  <i>Без задания — лежат у нас временно, {fmt(t['held_total'])} {CURRENCY}</i>")
        for name, pos in held.items():
            lines.append(f"• {name}: {fmt(_amount(pos))} {CURRENCY}  {_age(_verified(pos), today)}")

    assets = ledger.get("assets") or {}
    if assets:
        lines.append(f"\n💰 <b>Активы вне кошелька</b> — {fmt(t['assets_total'])} {CURRENCY}")
        for name, pos in assets.items():
            lines.append(f"• {name}: {fmt(_amount(pos))} {CURRENCY}  {_age(_verified(pos), today)}")

    recv = ledger.get("receivables") or {}
    if recv:
        lines.append(f"\n🤝 <b>Дебиторка</b> — {fmt(t['receivables_total'])} {CURRENCY}")
        for name, pos in recv.items():
            lines.append(f"• {name}: {fmt(_amount(pos))} {CURRENCY}  {_age(_verified(pos), today)}")

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
    """Первый запуск — кладём стартовые цифры (все несверенные). Дальше просто читаем."""
    if os.path.exists(_path(notes_dir, user_id)):
        return load(notes_dir, user_id)
    seeded = json.loads(json.dumps(SEED))
    save(notes_dir, user_id, seeded)
    return seeded


# ---------- Стартовое состояние ----------
# Цифры Ильи на 14.07.2026. Взяты из его расклада, НЕ пересчитаны физически —
# поэтому у каждой позиции verified=None: реестр честно показывает «не сверялось»,
# пока Илья не пересчитает сейф, крипту и не подтвердит остатки с людьми.
SEED = {
    "updated_at": "2026-07-14",
    "wallet": {
        "working": position(376_698),
        "held": {
            "Стефан": position(325_168),
            "Вадим": position(80_000),
            "Хайдар": position(8_116),
            "Макс": position(161_950),
            "Дмитрий Великий": position(137_098),
        },
    },
    "assets": {
        "Крипта": position(1_069_000),
        "Наличка": position(105_750),
        "Заморожено (счёт компании)": position(82_214),
    },
    "receivables": {
        "Фэйгэ": position(300_000),
        "Лэлэ": position(27_200),
        "Зама": position(20_000),
        "Степан": position(20_151),
        "Хасан": position(20_000),
    },
}

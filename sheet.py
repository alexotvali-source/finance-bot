"""
Хранение реестра в Google-таблице.

Таблица — единственная правда: её видно с телефона без бота, она переживает
Railway, у неё есть версии и бэкапы от Google.

Важное правило: если таблица недоступна, бот НЕ подставляет локальную копию
молча. Два источника правды = ноль источников правды — это мы уже проходили
(Cowork разошёлся с реестром на 387 351, и узнать об этом было неоткуда).
Лучше честная ошибка, чем тихо устаревшая цифра.

Локальная копия на волюме пишется после каждого успешного сохранения — но
только как резерв на случай катастрофы, читать её автоматически бот не будет.
"""

from __future__ import annotations

import json
import os

import requests

WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL", "")
SECRET = os.environ.get("SHEET_SECRET", "")


class SheetError(RuntimeError):
    """Таблица недоступна или отказала. Наверх уходит честной ошибкой."""


def enabled() -> bool:
    """Таблица настроена? Пока нет — бот работает на волюме."""
    return bool(WEBHOOK_URL and SECRET)


def load() -> dict | None:
    """Читает реестр из таблицы. None — таблица пуста (первый запуск)."""
    try:
        r = requests.get(WEBHOOK_URL, params={"secret": SECRET}, timeout=30)
        r.raise_for_status()
        res = r.json()
    except Exception as e:
        raise SheetError(f"не смог прочитать таблицу: {e}") from e
    if not res.get("ok"):
        raise SheetError(f"таблица отказала: {res.get('error')}")
    return res.get("ledger")


def save(ledger: dict, entries: list | None = None, backup_path: str | None = None) -> None:
    """Пишет реестр в таблицу. Локальная копия — только после успеха таблицы,
    чтобы резерв никогда не оказался новее правды.

    entries — строки журнала этой операции. Таблица их ДОПИСЫВАЕТ на отдельный лист;
    сюда шлём только новые, всю историю гонять незачем."""
    body = {"secret": SECRET, "ledger": ledger}
    if entries:
        body["log"] = entries
    try:
        r = requests.post(WEBHOOK_URL, json=body, timeout=30)
        r.raise_for_status()
        res = r.json()
    except Exception as e:
        raise SheetError(f"не смог записать в таблицу: {e}") from e
    if not res.get("ok"):
        raise SheetError(f"таблица не сохранила: {res.get('error')}")

    if backup_path:
        try:
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(ledger, f, ensure_ascii=False, indent=2)
        except Exception:
            # Резерв не критичен: правда уже в таблице. Молча не падаем.
            pass

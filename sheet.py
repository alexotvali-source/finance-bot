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
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL", "")
SECRET = os.environ.get("SHEET_SECRET", "")


class SheetError(RuntimeError):
    """Таблица недоступна или отказала. Наверх уходит честной ошибкой."""


def _safe(e) -> str:
    """Текст ошибки без секрета. requests кладёт в исключение полный URL, включая
    ?secret=..., и он утекал пользователю в чат. Вырезаем секрет и весь query."""
    msg = str(e)
    if SECRET:
        msg = msg.replace(SECRET, "***")
    return re.sub(r"([?&]secret=)[^&\s]+", r"\1***", msg)


def enabled() -> bool:
    """Таблица настроена? Пока нет — бот работает на волюме."""
    return bool(WEBHOOK_URL and SECRET)


def _get(params: dict, what: str) -> dict:
    """GET к таблице с повторами. Чтение идемпотентно — повторять безопасно.
    Apps Script на холодном старте иногда не укладывается в таймаут; один такой
    таймаут не должен превращаться в «не смог показать цифры»."""
    last = None
    for attempt in range(3):
        try:
            r = requests.get(WEBHOOK_URL, params=params, timeout=25)
            r.raise_for_status()
            res = r.json()
        except Exception as e:
            last = e
            log.warning("%s — попытка %d не удалась: %s", what, attempt + 1, e)
            time.sleep(1.5)
            continue
        # Логический отказ (forbidden и т.п.) повтором не лечится — сразу наверх.
        if not res.get("ok"):
            raise SheetError(f"таблица отказала: {res.get('error')}")
        return res
    raise SheetError(f"{what} за 3 попытки: {_safe(last)}")


def load() -> dict | None:
    """Читает реестр из таблицы. None — таблица пуста (первый запуск)."""
    return _get({"secret": SECRET}, "таблица не ответила").get("ledger")


def journal(limit: int = 15) -> list:
    """Последние строки журнала. Живут в таблице, а не в реестре."""
    return _get({"secret": SECRET, "log": limit}, "журнал не ответил").get("log") or []


def _content(ledger: dict | None) -> str:
    """Смысловая часть реестра без updated_at — чтобы сравнивать «применилось или нет»."""
    if not ledger:
        return ""
    core = {k: ledger.get(k) for k in ("wallet", "assets", "receivables", "expenses")}
    return json.dumps(core, sort_keys=True, ensure_ascii=False)


def _post_once(ledger: dict, entries: list | None) -> None:
    body = {"secret": SECRET, "ledger": ledger}
    if entries:
        body["log"] = entries
    r = requests.post(WEBHOOK_URL, json=body, timeout=30)
    r.raise_for_status()
    res = r.json()
    if not res.get("ok"):
        raise SheetError(f"таблица отказала: {res.get('error')}")


def save(ledger: dict, entries: list | None = None, backup_path: str | None = None) -> None:
    """Пишет реестр в таблицу. Локальная копия — только после успеха таблицы,
    чтобы резерв никогда не оказался новее правды.

    entries — строки журнала этой операции. Таблица их ДОПИСЫВАЕТ на отдельный лист;
    сюда шлём только новые, всю историю гонять незачем.

    Устойчивость к транзиентному сбою: Apps Script иногда отдаёт 404 на промежуточном
    редиректе googleusercontent, хотя запись УЖЕ прошла. Поэтому при ошибке сначала
    перечитываем таблицу: если реестр там уже равен нашему — считаем успехом и НЕ
    повторяем (иначе задвоили бы строки журнала). Не равен — один раз повторяем."""
    try:
        _post_once(ledger, entries)
    except Exception as e:
        log.warning("запись в таблицу не удалась (%s), проверяю, не применилось ли уже", e)
        applied = False
        try:
            applied = _content(load()) == _content(ledger)
        except Exception:
            applied = False
        if not applied:
            time.sleep(2)
            try:
                _post_once(ledger, entries)
            except Exception as e2:
                raise SheetError(f"таблица недоступна (повтор не помог): {_safe(e2)}") from e2

    if backup_path:
        try:
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(ledger, f, ensure_ascii=False, indent=2)
        except Exception:
            # Резерв не критичен: правда уже в таблице. Молча не падаем.
            pass

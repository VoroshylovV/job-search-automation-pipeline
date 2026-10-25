"""Діагностичний лог причин відсіву вакансій Кроку 1 (додано 27.09.2026).

Мета — порівняти онлайн- (промт) і десктоп- (цей репозиторій) версії за
URL: хто що відсіяв і на якому критерії. Кожен запуск створює окремий Google
Sheet у підпапці REJECT_LOG_FOLDER_ID з назвою
"Лог відсіву YYYY-MM-DD (тест гітхаб)" і пише всі рядки одним викликом.
Повторний запуск того ж дня — окремий файл «… (повтор N)».
Формат колонок (REJECT_LOG_HEADER) спільний з онлайн-версією.

З 26.10.2026: СПОЧАТКУ рядки пишуться в локальний logs/YYYY-MM-DD.csv (по джерелах),
потім вивантажуються в Drive з однією повторною спробою; після вивантаження файл
читається назад і кількість рядків порівнюється. Не збіглось/не вивантажено — результат
`verified=False` (main.py піднімає тривогу), локальна копія лишається.
Збій запису не валить запуск.
"""
from __future__ import annotations

import logging
from datetime import date

from config import REJECT_LOG_FOLDER_ID, REJECT_LOG_SYSTEM_LABEL, SOURCES
from google_services import drive, sheets
from google_services.auth import sheets_service
from models import REJECT_LOG_HEADER
from pipeline import local_log

logger = logging.getLogger(__name__)


def build_rows(decisions: list[dict], utc_today: date) -> list[list[str]]:
    return [
        [
            utc_today.isoformat(), REJECT_LOG_SYSTEM_LABEL, d["source"], d["url"], d["label"],
            d["result"], d["code"], d["detail"], d["posted_date"], d["date_source"], d.get("category", "—"),
            d.get("verdict_haiku", ""), d.get("verdict_sonnet", ""),
        ]
        for d in decisions
    ]


def drive_title(utc_today: date, n: int = 0) -> str:
    base = f"Лог відсіву {utc_today.isoformat()} ({REJECT_LOG_SYSTEM_LABEL})"
    return base if n == 0 else f"{base} (повтор {n})"


def _by_source(rows: list[list[str]]) -> list[list[list[str]]]:
    """Рядки згруповані по джерелах (порядок config.SOURCES, решта наприкінці)."""
    order = {s: i for i, s in enumerate(SOURCES)}
    groups: dict[str, list[list[str]]] = {}
    for r in rows:
        groups.setdefault(r[2], []).append(r)
    return [groups[k] for k in sorted(groups, key=lambda s: (order.get(s, len(order)), s))]


def write_reject_log(decisions: list[dict], utc_today: date) -> dict:
    """Локальний файл -> Drive (+1 повтор) -> перевірка кількості рядків.
    Повертає {local_path, run_index, title, rows, file_id, verified, error}."""
    rows = build_rows(decisions, utc_today)
    n = local_log.next_run_index(
        utc_today, lambda i: drive.find_file_by_title(drive_title(utc_today, i), REJECT_LOG_FOLDER_ID) is not None
    )
    title = drive_title(utc_today, n)
    runlog = local_log.RunLog(utc_today, n, REJECT_LOG_HEADER)
    for group in _by_source(rows):
        runlog.append_rows(group)
    result = {"local_path": str(runlog.path), "run_index": n, "title": title, "rows": len(rows),
              "file_id": None, "verified": False, "error": ""}

    values = [REJECT_LOG_HEADER] + rows
    state: dict[str, str] = {}

    def upload() -> None:
        if "id" not in state:  # повтор не створює другий файл
            state["id"] = drive.create_spreadsheet(title, REJECT_LOG_FOLDER_ID)
        sheets_service().spreadsheets().values().update(
            spreadsheetId=state["id"], range="A1", valueInputOption="RAW", body={"values": values},
        ).execute()

    try:
        local_log.retry_once(upload, "Вивантаження логу відсіву в Drive")
        result["file_id"] = state["id"]
        got = len(sheets.read_values(state["id"], "A:A"))
        if got != len(values):
            result["error"] = f"кількість рядків не збіглась: локально {len(values)} (з заголовком), у Drive {got}"
        else:
            result["verified"] = True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Лог причин відсіву не вивантажено в Drive")
        result["file_id"] = state.get("id")
        result["error"] = str(exc)
    return result

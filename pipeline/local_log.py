"""Локальні логи запуску (26.10.2026): спершу пишемо на диск, потім вивантажуємо в Drive.

- logs/YYYY-MM-DD.csv — лог відсіву запуску (ті самі колонки, що REJECT_LOG_HEADER),
  дописується по джерелах; повторний запуск того ж дня — «YYYY-MM-DD (повтор N).csv».
- logs/source_counts.csv — скільки карток дало кожне джерело (база для тривоги «0 карток»).
- logs/sheet_rows.csv — рядок метрики/перевірки, який не вдалося записати в Drive.
Локальні файли не видаляються."""
from __future__ import annotations

import csv
import json
import logging
import re
import statistics
import time
from datetime import date
from pathlib import Path
from typing import Callable, TypeVar

import config

logger = logging.getLogger(__name__)

LOGS_DIR = config.LOGS_DIR
RETRY_SLEEP_SEC = 2.0
T = TypeVar("T")

SOURCE_COUNTS_FILE = "source_counts.csv"
SOURCE_COUNTS_HEADER = ["date_utc", "source", "cards", "status", "recorded_utc"]
SHEET_ROWS_FILE = "sheet_rows.csv"


def logs_dir() -> Path:
    path = Path(LOGS_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def retry_once(fn: Callable[[], T], what: str) -> T:
    """Одна повторна спроба; друга помилка піднімається."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: збій (%s) — одна повторна спроба", what, exc)
        time.sleep(RETRY_SLEEP_SEC)
        return fn()


def run_log_name(day: date | str, n: int) -> str:
    day = day if isinstance(day, str) else day.isoformat()
    return f"{day}.csv" if n == 0 else f"{day} (повтор {n}).csv"


def next_run_index(day: date, drive_exists: Callable[[int], bool] = lambda n: False) -> int:
    """0 для першого запуску дня, далі 1, 2… — перше N, для якого немає ні локального
    файлу, ні файлу в Drive (drive_exists; його збій не рахується)."""
    n = 0
    while (logs_dir() / run_log_name(day, n)).exists() or _safe(drive_exists, n):
        n += 1
    return n


def _safe(fn: Callable[[int], bool], n: int) -> bool:
    try:
        return bool(fn(n))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Перевірка файлу в Drive не вдалась (%s) — рахую лише локальні файли", exc)
        return False


class RunLog:
    """Локальний CSV запуску; кожен append_rows зразу пишеться на диск."""

    def __init__(self, day: date, n: int, header: list[str]):
        self.path = logs_dir() / run_log_name(day, n)
        with open(self.path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(header)

    def append_rows(self, rows: list[list]) -> None:
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(rows)
            f.flush()


def save_row_fallback(kind: str, row: list, reason: str) -> Path:
    """Рядок, який не потрапив у Drive, лишається локально."""
    path = logs_dir() / SHEET_ROWS_FILE
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["saved_utc", "kind", "reason", "row_json"])
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), kind, reason[:200], json.dumps(row, ensure_ascii=False)])
    return path


# --------------------------------------------------------------------------
# Кількість карток по джерелах і тривога «0 там, де зазвичай десятки»
# --------------------------------------------------------------------------
def _read_source_counts() -> list[dict]:
    path = logs_dir() / SOURCE_COUNTS_FILE
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def source_baseline(source: str) -> float | None:
    """Медіана карток за останні ALARM_BASELINE_RUNS успішних запусків джерела;
    None, якщо записів менше ALARM_BASELINE_MIN_RUNS."""
    vals = []
    for r in _read_source_counts():
        if r.get("source") == source and r.get("status") == "OK":
            try:
                vals.append(int(r["cards"]))
            except (KeyError, ValueError):
                continue
    vals = vals[-config.ALARM_BASELINE_RUNS:]
    return statistics.median(vals) if len(vals) >= config.ALARM_BASELINE_MIN_RUNS else None


def zero_cards_anomaly(source: str, count: int) -> str:
    """Причина тривоги, якщо джерело дало 0 карток, хоча зазвичай десятки."""
    if count != 0:
        return ""
    base = source_baseline(source)
    if base is not None and base >= config.ALARM_ZERO_CARDS_BASELINE_MIN:
        return f"0 карток, хоча зазвичай ≈{base:g} (зміна верстки, блокування або збій джерела)"
    return ""


def record_source_count(day: date, source: str, count: int, status: str) -> None:
    """Дописує факт про джерело ОДРАЗУ після його скрейпінгу (по ходу роботи)."""
    try:
        path = logs_dir() / SOURCE_COUNTS_FILE
        new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(SOURCE_COUNTS_HEADER)
            w.writerow([day.isoformat(), source, count, status, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())])
    except OSError as exc:
        logger.warning("Не вдалось записати %s: %s", SOURCE_COUNTS_FILE, exc)

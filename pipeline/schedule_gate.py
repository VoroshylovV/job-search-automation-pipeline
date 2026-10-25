"""Розклад за часом Europe/Warsaw (26.10.2026), а не UTC і не поясом ПК.

Task Scheduler запускає `main.py --scheduled` кожні 15 хв (scripts/register_windows_task.ps1);
запуск відбувається, лише коли у Варшаві вже ≥ SCHEDULE_RUN_AT_LOCAL і сьогодні (за
Варшавою) ще не було запланованого запуску. Так розклад не залежить від часового поясу
ПК і переходів на літній/зимовий час. Ручний `python main.py` гейт не перевіряє."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from pipeline import local_log


def _state_path() -> Path:
    return local_log.logs_dir() / config.SCHEDULE_STATE_FILE


def _run_at() -> tuple[int, int]:
    hh, mm = config.SCHEDULE_RUN_AT_LOCAL.split(":")
    return int(hh), int(mm)


def is_due(now_utc: datetime | None = None) -> tuple[bool, str]:
    """(пора запускати?, пояснення для логу)."""
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    local = now_utc.astimezone(ZoneInfo(config.CANDIDATE_LOCAL_TZ))
    hh, mm = _run_at()
    if (local.hour, local.minute) < (hh, mm):
        return False, f"у {config.CANDIDATE_LOCAL_TZ} {local:%H:%M} — ще раніше за {config.SCHEDULE_RUN_AT_LOCAL}"
    path = _state_path()
    last = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if last == local.date().isoformat():
        return False, f"сьогодні ({last}, {config.CANDIDATE_LOCAL_TZ}) запланований запуск уже був"
    return True, f"{local:%Y-%m-%d %H:%M} {config.CANDIDATE_LOCAL_TZ} ≥ {config.SCHEDULE_RUN_AT_LOCAL}"


def describe(now_utc: datetime | None = None) -> tuple[bool, str]:
    """Рішення розкладу для `--scheduled --check-only`: (потрібен?, текст з часом у Варшаві)."""
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    local = now_utc.astimezone(ZoneInfo(config.CANDIDATE_LOCAL_TZ))
    due, why = is_due(now_utc)
    head = (f"Час: {local:%Y-%m-%d %H:%M} {config.CANDIDATE_LOCAL_TZ} (UTC {now_utc:%H:%M}); "
            f"розклад: {config.SCHEDULE_RUN_AT_LOCAL} {config.CANDIDATE_LOCAL_TZ}.")
    return due, f"{head}\n{'ЗАПУСК ПОТРІБЕН' if due else 'ЗАПУСК НЕ ПОТРІБЕН'}, бо {why}."


def mark_started(now_utc: datetime | None = None) -> None:
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    local = now_utc.astimezone(ZoneInfo(config.CANDIDATE_LOCAL_TZ))
    _state_path().write_text(local.date().isoformat(), encoding="utf-8")

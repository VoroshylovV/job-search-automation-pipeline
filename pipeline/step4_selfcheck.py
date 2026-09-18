"""Крок 4 — самоперевірка запуску.

Виконується завжди, незалежно від того, чи Кроки 1-3 завершились успішно,
частково чи провалились повністю. Нічого не перераховує заново на
джерелах і не змінює файли Кроків 1-3 — лише фіксує те, що вже фактично
сталося в цьому запуску (4.1-4.4), плюс одне читання власної короткої
історії для евристики 4.3.

ВАЖЛИВЕ й чесне обмеження цієї Python-реалізації: "Крок 3 (додатковий
блок)" чат-версії промпту (оновлення таблиці "Ворошилов відгуки на
вакансії" на основі фактів із розмовної пам'яті користувача) тут НЕ
реалізовано — і структурно не може бути реалізовано автономним cron-
скриптом: він читає /areas/job-search.md та інші memory-файли акаунту
Claude, до яких у самостійного Python-процесу просто немає доступу (це
не Google API, а внутрішня пам'ять асистента). step3_tracker_status тому
завжди "НЕ ВИКОНАНО" з поясненням — не помилка, а задокументована межа
архітектури. Таблицю відгуків Володимир або оновлює вручну, або просить
Claude оновити її окремо в чаті.
"""
from __future__ import annotations

import logging

from config import (
    LOW_MATCH_STREAK_HEURISTIC_RUNS,
    RESUME_FOLDER_ID,
    SELFCHECK_SHEET_TITLE,
    SELFCHECK_TREND_LOOKBACK_ROWS,
)
from google_services import drive, sheets
from models import SELFCHECK_HEADER, SelfCheckResult

logger = logging.getLogger(__name__)

TRACKER_NOT_IMPLEMENTED_NOTE = (
    "оновлення таблиці відгуків з розмовної пам'яті не реалізоване в "
    "автономному Python-пайплайні (немає доступу до memory-файлів чату) — "
    "онови таблицю вручну або окремим запитом у чаті"
)


def _step1_status(step1_result: dict) -> tuple[str, str]:
    statuses = step1_result.get("source_statuses", {})
    ok_count = sum(1 for s in statuses.values() if s.status == "OK")
    total = len(statuses) or 5
    dedup_level = step1_result.get("dedup_level_used", 0)

    notes = []
    if ok_count < total:
        notes.append(f"джерел перевірено {ok_count}/{total}")
    if dedup_level == 0:
        notes.append("дедублікація не виконана (обидва рівні недоступні)")
    elif dedup_level == 2:
        notes.append("спрацював лише рівень 2 дедублікації (таблиця відгуків)")

    if ok_count == total and dedup_level in (1, 2):
        status = "OK" if dedup_level == 1 else "ЧАСТКОВО"
    elif ok_count == 0:
        status = "НЕ ВИКОНАНО"
    else:
        status = "ЧАСТКОВО"
    return status, "; ".join(notes)


def _step2_status(step2_result: dict) -> tuple[str, str]:
    if step2_result.get("error"):
        return "НЕ ВИКОНАНО", str(step2_result["error"])
    if "company_names" not in step2_result and step2_result.get("total_emails_found", 0) == 0:
        # run_step2() рано вийшов (raw_emails порожній) — це не обов'язково
        # збій, могло просто не бути листів; company_names відсутнє лише
        # коли перелік компаній не витягувався взагалі.
        return "ЧАСТКОВО", "не вдалось підтвердити перелік компаній із Drive"
    return "OK", ""


def _step3_metrics_status(step3_result: dict) -> tuple[str, str]:
    if step3_result.get("saved"):
        return "OK", ""
    return "НЕ ВИКОНАНО", str(step3_result.get("error", ""))


def _collection_method(collection_methods: dict[str, str], source_statuses: dict) -> str:
    ok_methods = {
        collection_methods.get(name, "не_встановлено")
        for name, s in source_statuses.items()
        if s.status == "OK"
    }
    if not ok_methods:
        return "не_встановлено"
    if len(ok_methods) == 1:
        return next(iter(ok_methods))
    return "оцінка"  # неоднорідні методи збору серед джерел цього запуску


def _conversion_pct(total_found: int, shown: int) -> str:
    if total_found == 0:
        return "н/д"
    return str(round(shown / total_found * 100))


def _get_or_create_selfcheck_sheet() -> str:
    existing = drive.find_file_by_title(SELFCHECK_SHEET_TITLE, RESUME_FOLDER_ID)
    if existing:
        return existing["id"]
    sheet_id = drive.create_spreadsheet(SELFCHECK_SHEET_TITLE, RESUME_FOLDER_ID)
    sheets.ensure_header(sheet_id, header=SELFCHECK_HEADER)
    return sheet_id


def _low_match_streak(
    sheet_id: str, current_signal: str
) -> bool:
    """4.3 евристика: 3+ запуски поспіль (включно з поточним) з "так" у
    колонці "Лише Low/нуль при 5/5". Порядок непорівнюваних/невідомих
    ("н/д", порожньо) розриває серію — консервативно, щоб не спрацьовувати
    хибно на неповній історії."""
    if current_signal != "так":
        return False
    try:
        rows = sheets.read_last_data_rows(sheet_id, SELFCHECK_TREND_LOOKBACK_ROWS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не вдалось прочитати історію Кроку 4 для евристики: %s", exc)
        return False

    idx = SELFCHECK_HEADER.index("Лише Low/нуль при 5/5")
    streak = 1  # поточний запуск уже "так"
    for row in reversed(rows):  # від найновішого історичного рядка назад
        value = row[idx] if idx < len(row) else ""
        if value.strip() == "так":
            streak += 1
        else:
            break
    return streak >= LOW_MATCH_STREAK_HEURISTIC_RUNS


def run_step4(
    step1_result: dict,
    step2_result: dict,
    step3_result: dict,
    timestamp_utc: str,
) -> dict:
    step1_status, step1_note = _step1_status(step1_result)
    step2_status, step2_note = _step2_status(step2_result)
    step3_metrics_status, step3_metrics_note = _step3_metrics_status(step3_result)

    source_statuses = step1_result.get("source_statuses", {})
    sources_ok_count = sum(1 for s in source_statuses.values() if s.status == "OK")
    collection_method = _collection_method(
        step1_result.get("collection_methods", {}), source_statuses
    )

    total_found = step1_result.get("total_found_before_filters", 0)
    shown = len(step1_result.get("vacancies", []))
    conversion_pct = _conversion_pct(total_found, shown)

    trend_comparable = sources_ok_count == 5
    trend_reason = "" if trend_comparable else f"джерел перевірено {sources_ok_count}/5, а не 5/5"

    vacancies = step1_result.get("vacancies", [])
    full_coverage = sources_ok_count == 5
    if not full_coverage:
        only_low_or_zero = "н/д"
    elif not vacancies:
        only_low_or_zero = "так"
    else:
        only_low_or_zero = "так" if all(v.match_level == "Low" for v in vacancies) else "ні"

    result = SelfCheckResult(
        timestamp_utc=timestamp_utc,
        step1_status=step1_status,
        step1_note=step1_note,
        step2_status=step2_status,
        step2_note=step2_note,
        step3_metrics_status=step3_metrics_status,
        step3_metrics_note=step3_metrics_note,
        step3_tracker_status="НЕ ВИКОНАНО",
        step3_tracker_note=TRACKER_NOT_IMPLEMENTED_NOTE,
        sources_ok_count=sources_ok_count,
        collection_method=collection_method,
        conversion_pct=conversion_pct,
        trend_comparable=trend_comparable,
        trend_comparable_reason=trend_reason,
        only_low_or_zero_at_full_coverage=only_low_or_zero,
        low_match_streak_signal=False,  # оновиться нижче, після читання історії
    )

    saved = False
    save_error = ""
    try:
        sheet_id = _get_or_create_selfcheck_sheet()
        sheets.ensure_header(sheet_id, header=SELFCHECK_HEADER)
        result.low_match_streak_signal = _low_match_streak(sheet_id, only_low_or_zero)
        sheets.append_row(sheet_id, result.as_row())
        saved = True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не вдалось зберегти результат Кроку 4")
        save_error = str(exc)

    return {"result": result, "saved": saved, "error": save_error}


def format_selfcheck_block(result: SelfCheckResult) -> str:
    lines = [
        f"Перевірка запуску {result.timestamp_utc}",
        f"Крок 1: {result.step1_status}" + (f" — {result.step1_note}" if result.step1_note else " — без зауважень"),
        f"Крок 2: {result.step2_status}" + (f" — {result.step2_note}" if result.step2_note else " — без зауважень"),
        f"Крок 3 (метрика): {result.step3_metrics_status}"
        + (f" — {result.step3_metrics_note}" if result.step3_metrics_note else " — без зауважень"),
        f"Крок 3 (таблиця відгуків): {result.step3_tracker_status} — {result.step3_tracker_note}",
        f"Джерела: {result.sources_ok_count}/5 штатно",
        f"Спосіб збору «знайдено»: {result.collection_method}",
        f"Конверсія показано/знайдено: {result.conversion_pct}"
        + ("%" if result.conversion_pct != "н/д" else ""),
        "Порівнюваність тренду: "
        + ("так" if result.trend_comparable else f"ні ({result.trend_comparable_reason})"),
    ]
    if result.low_match_streak_signal:
        lines.append(
            f"⚠️ Сигнал (евристика): {LOW_MATCH_STREAK_HEURISTIC_RUNS}+ запуски поспіль лише "
            "Low/нуль при повному 5/5 покритті — варто переглянути фільтр досвіду/ЗП вручну "
            "(не обов'язково ознака ринку)."
        )
    return "\n".join(lines)

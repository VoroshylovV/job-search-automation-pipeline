"""Крок 5 — порівняння автоматичного (чат-версія, WebFetch) і ручного
(Python, requests+BeautifulSoup) запуску того самого дня.

Активний ЛИШЕ в тестовому режимі (config.SERVICE_FILE_TITLE_SUFFIX
непорожній, README "Тестовий режим") — доки довіра до Python-версії не
підтверджена і вона не перейняла канонічний журнал. Коли суфікс порожній,
run_step5() одразу повертає {"active": False}: порівнювати канонічний
журнал сам із собою немає сенсу.

Джерело "автоматичних" даних — КАНОНІЧНІ (без суфікса) Google-файли, якими
й далі керує щоденна scheduled-задача: обидва дедуп-логи, обидві таблиці
метрик, самоперевірка Кроку 4. Ці файли лише ЧИТАЮТЬСЯ, ніколи не
пишуться — так само, як TRACKER_SHEET_TITLE (Рівень 2 дедупу Кроку 1).

Джерело "ручних" даних — уже готові результати ЦЬОГО python-запуску
(step1_result, step1_2_result, step4_result), передані з main.py; читати
власні щойно записані файли вдруге не треба.

Результат — один рядок на день у "Порівняльна таблиця автоматичних
запусків та запусків вручну": знайдено/показано з обох боків, перетин
URL (скільки вакансій/проєктів показали обидві системи), скільки показала
лише одна сторона, і статус Кроку 4 з обох боків.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from config import (
    COMPARISON_SHEET_TITLE,
    DEDUP_LOG_TITLE_CANONICAL,
    FREELANCE_DEDUP_LOG_TITLE_CANONICAL,
    FREELANCE_METRICS_SHEET_TITLE_CANONICAL,
    METRICS_SHEET_TITLE_CANONICAL,
    RESUME_FOLDER_ID,
    SELFCHECK_SHEET_TITLE_CANONICAL,
    SERVICE_FILE_TITLE_SUFFIX,
)
from google_services import docs, drive, sheets
from models import (
    COMPARISON_HEADER,
    FREELANCE_METRICS_HEADER,
    METRICS_HEADER,
    SELFCHECK_HEADER,
    ComparisonRow,
)
from pipeline.step1_2_freelance import _parse_dedup_log as _parse_freelance_dedup_log
from pipeline.step1_vacancies import _parse_dedup_log as _parse_vacancy_dedup_log

logger = logging.getLogger(__name__)


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _canonical_metrics_row_today(title: str, today_str: str) -> list[str] | None:
    """Останній рядок канонічної таблиці метрик, чия перша колонка
    (дата запуску) дорівнює today_str. None — файл не знайдено, порожній,
    або сьогоднішнього рядка ще немає (автозапуск не проводився/провалився)."""
    file = drive.find_file_by_title(title, RESUME_FOLDER_ID)
    if not file:
        return None
    try:
        rows = sheets.read_values(file["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не вдалось прочитати канонічну таблицю метрик %r: %s", title, exc)
        return None
    data_rows = rows[1:] if len(rows) > 1 else []
    matching = [r for r in data_rows if r and r[0].startswith(today_str)]
    return matching[-1] if matching else None


def _canonical_selfcheck_row_today(today_str: str) -> list[str] | None:
    file = drive.find_file_by_title(SELFCHECK_SHEET_TITLE_CANONICAL, RESUME_FOLDER_ID)
    if not file:
        return None
    try:
        rows = sheets.read_values(file["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не вдалось прочитати канонічну самоперевірку: %s", exc)
        return None
    data_rows = rows[1:] if len(rows) > 1 else []
    # "Перевірка" — повна мітка часу "YYYY-MM-DD HH:MM UTC", тому теж startswith.
    matching = [r for r in data_rows if r and r[0].startswith(today_str)]
    return matching[-1] if matching else None


def _vacancy_urls_today(today_str: str) -> set[str] | None:
    file = drive.find_file_by_title(DEDUP_LOG_TITLE_CANONICAL, RESUME_FOLDER_ID)
    if not file:
        return None
    try:
        text = docs.read_full_text(file["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не вдалось прочитати канонічний лог вакансій: %s", exc)
        return None
    entries = _parse_vacancy_dedup_log(text)
    return {e.identifier for e in entries if e.shown_date == today_str and e.identifier.startswith("http")}


def _freelance_urls_today(today_str: str) -> set[str] | None:
    file = drive.find_file_by_title(FREELANCE_DEDUP_LOG_TITLE_CANONICAL, RESUME_FOLDER_ID)
    if not file:
        return None
    try:
        text = docs.read_full_text(file["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не вдалось прочитати канонічний лог фрілансу: %s", exc)
        return None
    entries = _parse_freelance_dedup_log(text)
    return {e.url for e in entries if e.shown_date == today_str}


def _get_or_create_comparison_sheet() -> str:
    existing = drive.find_file_by_title(COMPARISON_SHEET_TITLE, RESUME_FOLDER_ID)
    if existing:
        return existing["id"]
    sheet_id = drive.create_spreadsheet(COMPARISON_SHEET_TITLE, RESUME_FOLDER_ID)
    sheets.ensure_header(sheet_id, header=COMPARISON_HEADER)
    return sheet_id


def run_step5(
    step1_result: dict,
    step1_2_result: dict,
    step4_result: dict,
    utc_today: date | None = None,
) -> dict:
    if not SERVICE_FILE_TITLE_SUFFIX:
        return {"active": False}

    utc_today = utc_today or datetime.now(timezone.utc).date()
    today_str = utc_today.isoformat()

    # --- Крок 1: вакансії ---
    v_row = _canonical_metrics_row_today(METRICS_SHEET_TITLE_CANONICAL, today_str)
    v_found_idx = METRICS_HEADER.index(
        "Загальна кількість вакансій, знайдених на всіх джерелах до фільтрації"
    )
    v_shown_idx = METRICS_HEADER.index("Показані вакансії (пройшли всі фільтри)")
    v_found_auto = _int_or_none(v_row[v_found_idx]) if v_row and len(v_row) > v_found_idx else None
    v_shown_auto = _int_or_none(v_row[v_shown_idx]) if v_row and len(v_row) > v_shown_idx else None

    auto_v_urls = _vacancy_urls_today(today_str)
    manual_v_urls = {v.url for v in step1_result.get("vacancies", []) if v.url}
    if auto_v_urls is None:
        v_url_overlap = v_url_only_auto = v_url_only_manual = None
    else:
        v_url_overlap = len(auto_v_urls & manual_v_urls)
        v_url_only_auto = len(auto_v_urls - manual_v_urls)
        v_url_only_manual = len(manual_v_urls - auto_v_urls)

    # --- Крок 1.2: фріланс ---
    f_row = _canonical_metrics_row_today(FREELANCE_METRICS_SHEET_TITLE_CANONICAL, today_str)
    f_shown_idx = FREELANCE_METRICS_HEADER.index("Показано разом")
    f_shown_auto = _int_or_none(f_row[f_shown_idx]) if f_row and len(f_row) > f_shown_idx else None

    auto_f_urls = _freelance_urls_today(today_str)
    manual_f_urls = {p.url for p in step1_2_result.get("projects", [])}
    f_url_overlap = len(auto_f_urls & manual_f_urls) if auto_f_urls is not None else None

    # --- Крок 4 ---
    sc_row = _canonical_selfcheck_row_today(today_str)
    sources_idx = SELFCHECK_HEADER.index("Джерела")
    step1_status_idx = SELFCHECK_HEADER.index("Крок 1")
    if sc_row and len(sc_row) > max(sources_idx, step1_status_idx):
        step4_sources_auto = sc_row[sources_idx]
        step4_status_auto = sc_row[step1_status_idx].split(" — ")[0]
    else:
        step4_sources_auto, step4_status_auto = "н/д", "н/д"

    manual_selfcheck = step4_result.get("result") if step4_result else None
    step4_sources_manual = f"{manual_selfcheck.sources_ok_count}/5" if manual_selfcheck else "н/д"
    step4_status_manual = manual_selfcheck.step1_status if manual_selfcheck else "н/д"

    notes_parts = []
    if v_row is None:
        notes_parts.append("автоматичні метрики вакансій за сьогодні недоступні")
    if f_row is None:
        notes_parts.append("автоматичні метрики фрілансу за сьогодні недоступні")
    if sc_row is None:
        notes_parts.append("автоматична самоперевірка за сьогодні недоступна")

    comparison = ComparisonRow(
        run_date=today_str,
        v_found_auto=v_found_auto,
        v_found_manual=step1_result.get("total_found_before_filters", 0),
        v_shown_auto=v_shown_auto,
        v_shown_manual=len(step1_result.get("vacancies", [])),
        v_url_overlap=v_url_overlap,
        v_url_only_auto=v_url_only_auto,
        v_url_only_manual=v_url_only_manual,
        f_shown_auto=f_shown_auto,
        f_shown_manual=len(step1_2_result.get("projects", [])),
        f_url_overlap=f_url_overlap,
        step4_sources_auto=step4_sources_auto,
        step4_sources_manual=step4_sources_manual,
        step4_status_auto=step4_status_auto,
        step4_status_manual=step4_status_manual,
        notes="; ".join(notes_parts),
    )

    saved = False
    save_error = ""
    try:
        sheet_id = _get_or_create_comparison_sheet()
        sheets.ensure_header(sheet_id, header=COMPARISON_HEADER)
        sheets.append_row(sheet_id, comparison.as_row())
        saved = True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не вдалось зберегти рядок Кроку 5 (порівняння)")
        save_error = str(exc)

    return {"active": True, "row": comparison, "saved": saved, "error": save_error}


def format_comparison_block(row: ComparisonRow) -> str:
    lines = [
        f"Крок 1 (вакансії) — знайдено: авто {row.v_found_auto if row.v_found_auto is not None else 'н/д'} "
        f"vs Python {row.v_found_manual}; показано: авто "
        f"{row.v_shown_auto if row.v_shown_auto is not None else 'н/д'} vs Python {row.v_shown_manual}",
    ]
    if row.v_url_overlap is not None:
        lines.append(
            f"Перетин URL: {row.v_url_overlap} спільних, {row.v_url_only_auto} лише в авто, "
            f"{row.v_url_only_manual} лише в Python"
        )
    else:
        lines.append("Перетин URL: н/д (канонічний лог вакансій за сьогодні недоступний)")
    lines.append(
        f"Крок 1.2 (фріланс) — показано: авто "
        f"{row.f_shown_auto if row.f_shown_auto is not None else 'н/д'} vs Python {row.f_shown_manual}"
        + (f"; перетин URL: {row.f_url_overlap}" if row.f_url_overlap is not None else "")
    )
    lines.append(
        f"Крок 4 — джерела: авто {row.step4_sources_auto} vs Python {row.step4_sources_manual}; "
        f"статус Кроку 1: авто {row.step4_status_auto} vs Python {row.step4_status_manual}"
    )
    if row.notes and row.notes != "без зауважень":
        lines.append(f"⚠️ {row.notes}")
    return "\n".join(lines)

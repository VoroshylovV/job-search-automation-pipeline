"""Крок 3 — метрики. Прямий values.append у Google Sheets — жодних
CSV-хитрощів і жодного trash+recreate, на відміну від MCP-версії."""
from __future__ import annotations

import logging
from datetime import date

from config import METRICS_SHEET_TITLE, RESUME_FOLDER_ID
from google_services import drive, sheets
from models import RunMetrics

logger = logging.getLogger(__name__)


def _get_or_create_metrics_sheet() -> str:
    existing = drive.find_file_by_title(METRICS_SHEET_TITLE, RESUME_FOLDER_ID)
    if existing:
        return existing["id"]
    sheet_id = drive.create_spreadsheet(METRICS_SHEET_TITLE, RESUME_FOLDER_ID)
    sheets.ensure_header(sheet_id)
    return sheet_id


def run_step3(
    step1_result: dict,
    step2_result: dict,
    today: date | None = None,
) -> dict:
    today = today or date.today()
    vacancies = step1_result["vacancies"]

    metrics = RunMetrics(
        run_date=today.isoformat(),
        total_found_before_filters=step1_result["total_found_before_filters"],
        shown_after_filters=len(vacancies),
        low_match_location_count=sum(1 for v in vacancies if v.low_match_location),
        veteran_bonus_count=sum(1 for v in vacancies if v.veteran_bonus),
        undetermined_date_count=sum(1 for v in vacancies if v.date_undetermined),
        total_emails_found=step2_result.get("total_emails_found", 0),
        hr_domain_emails=step2_result.get("hr_domain_emails", 0),
        known_company_emails=step2_result.get("known_company_emails", 0),
        source_statuses={
            name: s.status for name, s in step1_result["source_statuses"].items()
        },
    )

    notes = []
    if step1_result.get("dedup_log_note"):
        notes.append(step1_result["dedup_log_note"])
    metrics.notes = "; ".join(notes)

    try:
        sheet_id = _get_or_create_metrics_sheet()
        sheets.ensure_header(sheet_id)
        sheets.append_row(sheet_id, metrics.as_row())
        return {"metrics": metrics, "saved": True}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не вдалось зберегти метрику")
        return {"metrics": metrics, "saved": False, "error": str(exc)}

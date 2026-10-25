"""Крок 3 — метрики. Прямий values.append у Google Sheets — жодних
CSV-хитрощів і жодного trash+recreate, на відміну від MCP-версії."""
from __future__ import annotations

import logging
from datetime import date

from claude_orchestrator.cost import tracker as cost_tracker
from config import CLAUDE_MODEL_CARDS, CLAUDE_MODEL_CARDS_SHADOW, CLAUDE_MODEL_FULLTEXT, METRICS_SHEET_TITLE, RESUME_FOLDER_ID
from google_services import drive, sheets
from models import RunMetrics

logger = logging.getLogger(__name__)



def model_label(model_id: str) -> str:
    """«claude-sonnet-5-5» → «Sonnet 5.5», «claude-haiku-4-5-20251001» → «Haiku 4.5»
    (той самий формат, що вже вписано вручну в таблицю метрик)."""
    import re
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$", (model_id or "").strip())
    if not m:
        return model_id or ""
    return f"{m[1].capitalize()} {m[2]}" + (f".{m[3]}" if m[3] else "")

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
    utc_today: date | None = None,
) -> dict:
    from datetime import datetime, timezone

    utc_today = utc_today or datetime.now(timezone.utc).date()
    vacancies = step1_result["vacancies"]
    shadow = step1_result.get("shadow") or {}

    metrics = RunMetrics(
        # UTC — узгоджено з логом дублів і результатом Кроку 4 (див.
        # config.py, розділ "Часові пояси").
        run_date=utc_today.isoformat(),
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
        # Усі виклики Claude (Кроки 1, 1.2, 2) відбуваються до Кроку 3,
        # тож сума тут уже повна.
        cost_total_usd=cost_tracker.total_usd,
        cost_per_shown_usd=(cost_tracker.total_usd / len(vacancies)) if vacancies else None,
        model_cards=model_label(CLAUDE_MODEL_CARDS),
        model_fulltext=model_label(CLAUDE_MODEL_FULLTEXT),
        model_cards_shadow=model_label(CLAUDE_MODEL_CARDS_SHADOW) if shadow.get("enabled") else "",
        shadow_compared=shadow.get("compared", 0),
        shadow_agree=shadow.get("agree", 0),
        shadow_disagree=shadow.get("disagree", 0),
        shadow_sonnet_pass_haiku_reject=shadow.get("sonnet_pass_haiku_reject", 0),
        shadow_haiku_pass_sonnet_reject=shadow.get("haiku_pass_sonnet_reject", 0),
    )

    notes = []
    if step1_result.get("dedup_log_note"):
        notes.append(step1_result["dedup_log_note"])
    metrics.notes = "; ".join(notes)

    try:
        sheet_id = _get_or_create_metrics_sheet()
        sheets.ensure_header(sheet_id)
        sheets.upsert_row_by_first_cell(sheet_id, metrics.as_row())
        return {"metrics": metrics, "saved": True}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не вдалось зберегти метрику")
        return {"metrics": metrics, "saved": False, "error": str(exc)}

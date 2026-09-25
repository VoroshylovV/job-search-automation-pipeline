"""Крок 3 (додатковий блок) — оновлення таблиці "Ворошилов відгуки на
вакансії" за листами, знайденими Кроком 2.

Чат-версія оновлювала таблицю з розмовної пам'яті Claude — автономному
скрипту вона недоступна. Натомість тут джерело фактів — сама пошта: якщо
Крок 2 класифікував лист як "відмова" або "запрошення на співбесіду" і лист
ОДНОЗНАЧНО належить одній компанії з таблиці, рядок оновлюється.

Навмисно консервативно (краще пропустити, ніж зіпсувати ручну таблицю):
- пишуться лише 2 колонки: "Результат відгуку" (тільки -> "відмова") і
  "Примітки" (дописується рядок у кінець, старий текст не видаляється);
- запрошення НЕ змінює "Результат відгуку" — лише примітка;
- кілька рядків однієї компанії (напр. 2 вакансії Plamigo) і лист без
  явної вказівки на вакансію -> пропуск з поясненням у звіті;
- ідемпотентно: якщо тема листа вже є в примітках, рядок не чіпається
  (повторний запуск того ж дня нічого не дублює);
- вимикається одним прапорцем config.TRACKER_AUTO_UPDATE.
"""
from __future__ import annotations

import logging
import re
from email.utils import parsedate_to_datetime

from config import (
    RESUME_FOLDER_ID,
    TRACKER_AUTO_UPDATE,
    TRACKER_COMPANY_COLUMN_HEADER,
    TRACKER_NOTES_COLUMN_HEADER,
    TRACKER_RESULT_COLUMN_HEADER,
    TRACKER_SHEET_NAME,
    TRACKER_SHEET_TITLE,
)
from google_services import drive, sheets
from models import EmailFinding

logger = logging.getLogger(__name__)

STATUS_REJECTION = "відмова"
STATUS_INVITE = "запрошення на співбесіду"
ACTIONABLE_STATUSES = {STATUS_REJECTION, STATUS_INVITE}
MIN_TOKEN_LEN = 3


def _company_keys(company: str) -> tuple[str, str]:
    """(повна_основна_назва, перше_слово) у нижньому регістрі.
    "appflame (Hily)" -> ("appflame", "appflame");
    "PwC Service Delivery Center" -> ("pwc service delivery center", "pwc")."""
    main = re.sub(r"\(.*?\)", "", company).strip().lower()
    first = re.split(r"[\s,.\-]+", main)[0] if main else ""
    return main, first


def _sender_domain(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender)
    return m.group(1).lower() if m else ""


def _matches(company: str, finding: EmailFinding) -> bool:
    main, first = _company_keys(company)
    if len(main) < MIN_TOKEN_LEN:
        return False
    haystack = f"{finding.sender} {finding.subject}".lower()
    if main in haystack:
        return True
    domain = _sender_domain(finding.sender)
    return len(first) >= MIN_TOKEN_LEN and bool(domain) and re.search(
        rf"(^|\.){re.escape(first)}\.", domain
    ) is not None


def _fmt_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).strftime("%d.%m.%Y")
    except (TypeError, ValueError, IndexError):
        return raw or "дата невідома"


def plan_updates(rows: list[list[str]], findings: list[EmailFinding]) -> dict:
    """Чиста функція (без API): що і куди записати. Повертає
    {"updates": [(A1, value)], "applied": [str], "skipped": [str]}."""
    result: dict = {"updates": [], "applied": [], "skipped": []}
    located = sheets.find_header_row(rows, TRACKER_COMPANY_COLUMN_HEADER)
    if located is None:
        result["skipped"].append(f"не знайдено колонку «{TRACKER_COMPANY_COLUMN_HEADER}»")
        return result
    header_idx, company_col = located
    header = rows[header_idx]
    try:
        result_col = [c.strip() for c in header].index(TRACKER_RESULT_COLUMN_HEADER)
        notes_col = [c.strip() for c in header].index(TRACKER_NOTES_COLUMN_HEADER)
    except ValueError:
        result["skipped"].append("не знайдено колонки «Результат відгуку»/«Примітки»")
        return result

    # поточний стан клітинок, щоб кілька листів в одному запуску не
    # перетирали дописи один одного в "Примітки"
    notes_state: dict[int, str] = {}
    result_state: dict[int, str] = {}

    def cell(row: list[str], idx: int) -> str:
        return row[idx].strip() if idx < len(row) else ""

    for f in findings:
        if f.status not in ACTIONABLE_STATUSES:
            continue
        label = f"{f.sender} | {f.subject}"
        matched: dict[str, list[int]] = {}
        for r_idx in range(header_idx + 1, len(rows)):
            company = cell(rows[r_idx], company_col)
            if company and _matches(company, f):
                matched.setdefault(company, []).append(r_idx)
        if not matched:
            continue  # лист не про компанію з таблиці — не наша справа
        if len(matched) > 1:
            result["skipped"].append(f"{label}: підходить кілька компаній ({', '.join(matched)})")
            continue
        company, row_ids = next(iter(matched.items()))
        if len(row_ids) > 1:
            pending = [r for r in row_ids if result_state.get(r, cell(rows[r], result_col)) != STATUS_REJECTION]
            if len(pending) != 1:
                result["skipped"].append(
                    f"{label}: у таблиці {len(row_ids)} рядки «{company}» — "
                    "неоднозначно, яку вакансію стосується лист (онови вручну)"
                )
                continue
            row_ids = pending
        r_idx = row_ids[0]
        sheet_row = r_idx + 1  # A1-нотація 1-indexed
        notes = notes_state.get(r_idx, cell(rows[r_idx], notes_col))
        subject_key = f.subject.strip()
        if subject_key and subject_key in notes:
            continue  # уже записано раніше — ідемпотентність

        kind = "Відмова" if f.status == STATUS_REJECTION else "Запрошення на співбесіду"
        note_line = f"{kind} {_fmt_date(f.date)} (автопайплайн, лист: «{subject_key}»)"
        new_notes = f"{notes}; {note_line}" if notes else note_line
        notes_state[r_idx] = new_notes
        result["updates"].append((f"{sheets.col_letter(notes_col + 1)}{sheet_row}", new_notes))

        if f.status == STATUS_REJECTION and result_state.get(r_idx, cell(rows[r_idx], result_col)) != STATUS_REJECTION:
            result_state[r_idx] = STATUS_REJECTION
            result["updates"].append((f"{sheets.col_letter(result_col + 1)}{sheet_row}", STATUS_REJECTION))
        result["applied"].append(f"рядок {sheet_row} «{company}»: {kind.lower()}")
    return result


def run_step3_tracker(step2_result: dict) -> dict:
    """Повертає {"status", "note", "applied", "skipped"} для Кроку 4 і звіту."""
    if not TRACKER_AUTO_UPDATE:
        return {"status": "НЕ ВИКОНАНО", "note": "вимкнено (config.TRACKER_AUTO_UPDATE=False)", "applied": [], "skipped": []}

    findings = [f for f in step2_result.get("findings", []) if f.status in ACTIONABLE_STATUSES]
    if not findings:
        return {"status": "OK", "note": "без нових відмов/запрошень у пошті", "applied": [], "skipped": []}

    tracker = drive.find_file_by_title(TRACKER_SHEET_TITLE, RESUME_FOLDER_ID)
    if not tracker:
        return {"status": "НЕ ВИКОНАНО", "note": f"таблицю «{TRACKER_SHEET_TITLE}» не знайдено", "applied": [], "skipped": []}

    range_ = f"'{TRACKER_SHEET_NAME}'!A1:ZZ" if TRACKER_SHEET_NAME else "A1:ZZ"
    rows = sheets.read_values(tracker["id"], range_)
    plan = plan_updates(rows, findings)
    sheets.update_cells(tracker["id"], plan["updates"], sheet_name=TRACKER_SHEET_NAME or None)

    parts = []
    if plan["applied"]:
        parts.append("оновлено: " + "; ".join(plan["applied"]))
    if plan["skipped"]:
        parts.append("пропущено: " + "; ".join(plan["skipped"]))
    note = " | ".join(parts) or "листи не стосуються компаній з таблиці"
    status = "ЧАСТКОВО" if plan["skipped"] else "OK"
    logger.info("Крок 3 (таблиця відгуків): %s — %s", status, note)
    return {"status": status, "note": note, "applied": plan["applied"], "skipped": plan["skipped"]}

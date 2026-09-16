"""Крок 1 — нові вакансії.

Оркеструє: скрапінг 5 джерел -> Claude-оцінка (критерії/Match-рівень/
ветерани/локація/дата) -> дедуплікація проти лога (детерміновано, в Python)
-> сортування за Match-рівнем -> оновлення лога показаних вакансій.

Підрахунок "скільки всього знайдено" / "скільки показано" тут НЕ лічильники
"по ходу" (як у чат-версії) — це просто len() над готовими Python-списками,
тому проблема "модель збилась з рахунку на 50+ вакансіях" структурно не
може повторитись: рахує код, не LLM.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from claude_orchestrator.client import call_json
from claude_orchestrator.prompts import build_vacancy_eval_prompt
from config import DATE_WINDOW_DAYS, DEDUP_LOG_MAX_AGE_DAYS, DEDUP_LOG_TITLE, RESUME_FOLDER_ID
from google_services import docs, drive
from models import RawJobPosting, ScoredVacancy, SourceStatus
from scrapers import SCRAPER_MODULES
from scrapers.base import ScraperError

logger = logging.getLogger(__name__)

MATCH_ORDER = {"High": 0, "Medium": 1, "Low": 2}


@dataclass
class DedupEntry:
    shown_date: str
    identifier: str  # URL, або "(без прямого URL, source)"
    label: str  # "назва посади — компанія"


def _parse_dedup_log(text: str) -> list[DedupEntry]:
    entries: list[DedupEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        entries.append(DedupEntry(shown_date=parts[0], identifier=parts[1], label=parts[2]))
    return entries


def _get_or_create_dedup_doc() -> str:
    existing = drive.find_file_by_title(DEDUP_LOG_TITLE, RESUME_FOLDER_ID)
    if existing:
        return existing["id"]
    doc_id = drive.create_google_doc(DEDUP_LOG_TITLE, RESUME_FOLDER_ID)
    docs.replace_full_text(
        doc_id,
        "Лог показаних вакансій — службовий файл автоматичного пайплайна. "
        "Формат рядка: дата_показу | URL_або_інший_ідентифікатор | назва посади — компанія.\n",
    )
    return doc_id


def _is_duplicate(job_key: str, log_entries: list[DedupEntry]) -> bool:
    for entry in log_entries:
        if entry.identifier == job_key or entry.label.lower() == job_key.lower():
            return True
    return False


def _scrape_all(today: date) -> tuple[list[RawJobPosting], dict[str, SourceStatus]]:
    all_jobs: list[RawJobPosting] = []
    statuses: dict[str, SourceStatus] = {}
    for source_name, module in SCRAPER_MODULES.items():
        try:
            jobs = list(module.scrape())
            all_jobs.extend(jobs)
            statuses[source_name] = SourceStatus(source=source_name, status="OK")
            logger.info("%s: зібрано %d кандидатів", source_name, len(jobs))
        except ScraperError as exc:
            statuses[source_name] = SourceStatus(
                source=source_name, status="недоступне", note=str(exc)
            )
            logger.warning("%s недоступне: %s", source_name, exc)
    return all_jobs, statuses


def run_step1(today: date | None = None) -> dict:
    today = today or date.today()
    yesterday = today - timedelta(days=1)

    raw_jobs, source_statuses = _scrape_all(today)
    total_found_before_filters = len(raw_jobs)

    if not raw_jobs:
        return {
            "vacancies": [],
            "total_found_before_filters": 0,
            "source_statuses": source_statuses,
            "dedup_log_updated": False,
            "dedup_log_note": "",
        }

    # Claude оцінює всі зібрані кандидати одним викликом (батч) — за потреби
    # можна розбити на шматки по ~40 вакансій, якщо контекст завеликий.
    CHUNK = 40
    evaluations: list[dict] = []
    for start in range(0, len(raw_jobs), CHUNK):
        chunk = raw_jobs[start : start + CHUNK]
        prompt = build_vacancy_eval_prompt(chunk, today=today)
        result = call_json(prompt)
        for ev in result.get("evaluations", []):
            ev["raw_index"] += start  # зсув індексів під повний список
        evaluations.extend(result.get("evaluations", []))

    eval_by_index = {ev["raw_index"]: ev for ev in evaluations}

    dedup_doc_id = _get_or_create_dedup_doc()
    log_text = docs.read_full_text(dedup_doc_id)
    log_entries = _parse_dedup_log(log_text)

    scored: list[ScoredVacancy] = []
    for i, job in enumerate(raw_jobs):
        ev = eval_by_index.get(i)
        if not ev or not ev.get("passes_criteria"):
            continue

        posted_date = ev.get("posted_date")
        date_undetermined = bool(ev.get("date_undetermined"))
        if not date_undetermined and posted_date:
            try:
                parsed = datetime.fromisoformat(posted_date).date()
            except ValueError:
                date_undetermined = True
                parsed = None
            if parsed and parsed not in (today, yesterday):
                continue  # поза вікном "останні 2 дні" — не показуємо

        vacancy = ScoredVacancy(
            title=ev.get("normalized_title") or job.title,
            company=ev.get("normalized_company") or job.company,
            source=job.source,
            url=job.url,
            match_level=ev.get("match_level") or "Low",
            match_reasoning=ev.get("match_reasoning") or "",
            veteran_bonus=bool(ev.get("veteran_bonus")),
            low_match_location=bool(ev.get("low_match_location")),
            low_match_location_reason=ev.get("low_match_location_reason") or "",
            posted_date=posted_date,
            date_undetermined=date_undetermined,
        )

        if _is_duplicate(vacancy.dedup_key(), log_entries):
            continue

        scored.append(vacancy)

    scored.sort(key=lambda v: MATCH_ORDER.get(v.match_level, 3))

    # Оновлення лога: старі рядки (не старші DEDUP_LOG_MAX_AGE_DAYS) + нові.
    dedup_log_updated = False
    dedup_log_note = ""
    try:
        cutoff = today - timedelta(days=DEDUP_LOG_MAX_AGE_DAYS)
        kept_lines = []
        for entry in log_entries:
            try:
                entry_date = datetime.fromisoformat(entry.shown_date).date()
            except ValueError:
                continue
            if entry_date >= cutoff:
                kept_lines.append(f"{entry.shown_date} | {entry.identifier} | {entry.label}")

        new_lines = [
            f"{today.isoformat()} | {v.url or f'(без прямого URL, {v.source})'} | {v.title} — {v.company}"
            for v in scored
        ]

        header = (
            "Лог показаних вакансій — службовий файл автоматичного пайплайна. "
            "Формат рядка: дата_показу | URL_або_інший_ідентифікатор | назва посади — компанія.\n"
        )
        full_content = header + "\n".join(kept_lines + new_lines) + ("\n" if kept_lines or new_lines else "")
        docs.replace_full_text(dedup_doc_id, full_content)
        dedup_log_updated = True
    except Exception as exc:  # noqa: BLE001
        dedup_log_note = f"лог дублів не вдалось оновити: {exc}"
        logger.exception("Не вдалось оновити лог дублів")

    return {
        "vacancies": scored,
        "total_found_before_filters": total_found_before_filters,
        "source_statuses": source_statuses,
        "dedup_log_updated": dedup_log_updated,
        "dedup_log_note": dedup_log_note,
    }

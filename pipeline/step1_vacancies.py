"""Крок 1 — нові вакансії.

Оркеструє: скрапінг 5 джерел -> Claude-оцінка (критерії/Match-рівень/
ветерани/локація/дата) -> дедуплікація (детерміновано, в Python; дворівнева
страховка — див. нижче) -> сортування за Match-рівнем -> оновлення лога
показаних вакансій.

Підрахунок "скільки всього знайдено" / "скільки показано" тут НЕ лічильники
"по ходу" (як у чат-версії) — це просто len() над готовими Python-списками,
тому проблема "модель збилась з рахунку на 50+ вакансіях" структурно не
може повторитись: рахує код, не LLM.

Два навмисно РІЗНІ "сьогодні" (див. config.py, розділ "Часові пояси"):
  - utc_today — дата запуску скрипта в UTC, іде в рядки лога дублів (щоб
    його можна було напряму зіставляти з метриками й результатом Кроку 4);
  - local_today — дата за місцевим часом кандидата (Europe/Warsaw), іде
    ЛИШЕ в оцінку "сьогодні/вчора" для дати ПУБЛІКАЦІЇ вакансії.
Зазвичай це один і той самий календарний день, окрім вузького вікна після
півночі UTC, але не за півночі Варшави (CET/CEST) — тому вони НЕ повинні
бути одним параметром, навіть якщо на практиці найчастіше збігаються.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from claude_orchestrator.client import call_json
from claude_orchestrator.prompts import build_vacancy_eval_prompt
from config import (
    CANDIDATE_LOCAL_TZ,
    DATE_WINDOW_DAYS,
    DEDUP_LOG_MAX_AGE_DAYS,
    DEDUP_LOG_TITLE,
    RESUME_FOLDER_ID,
    TRACKER_SHEET_TITLE,
    TRACKER_URL_COLUMN_HEADER,
)
from google_services import docs, drive, sheets
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
        "Формат рядка: дата_показу (UTC) | URL_або_інший_ідентифікатор | назва посади — компанія.\n",
    )
    return doc_id


def _is_duplicate(job_key: str, log_entries: list[DedupEntry]) -> bool:
    for entry in log_entries:
        if entry.identifier == job_key or entry.label.lower() == job_key.lower():
            return True
    return False


def _read_dedup_log_with_retry() -> tuple[str | None, str | None]:
    """Повертає (doc_id, text). text=None, якщо не вдалось прочитати навіть
    після однієї повторної спроби — сигнал для дворівневої страховки нижче."""
    doc_id = None
    for attempt in range(2):
        try:
            doc_id = _get_or_create_dedup_doc()
            text = docs.read_full_text(doc_id)
            return doc_id, text
        except Exception as exc:  # noqa: BLE001
            logger.warning("Спроба %d читання лога дублів провалилась: %s", attempt + 1, exc)
    return doc_id, None


def _level2_tracker_urls() -> tuple[set[str] | None, str]:
    """Рівень 2 страховки: ЛИШЕ читання URL-колонки таблиці "Ворошилов
    відгуки на вакансії". Повертає (None, note) якщо й ця таблиця
    недоступна. НЕ звіряє за назвою компанії — див. коментар у config.py."""
    tracker = drive.find_file_by_title(TRACKER_SHEET_TITLE, RESUME_FOLDER_ID)
    if not tracker:
        return None, (
            "Обидва рівні дедублікації (лог і таблиця відгуків) виявились "
            "недоступні в цьому запуску — показ вакансій без дедублікації."
        )
    try:
        urls = sheets.read_column_by_header(tracker["id"], TRACKER_URL_COLUMN_HEADER)
        return set(urls), (
            "Лог дублів недоступний; дедублікацію виконано по колонці "
            f"«{TRACKER_URL_COLUMN_HEADER}» таблиці відгуків (охоплює лише "
            "вакансії, на які подано, — показані-але-не-подані могли пройти повторно)."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Рівень 2 дедублікації (таблиця відгуків) недоступний: %s", exc)
        return None, (
            "Обидва рівні дедублікації (лог і таблиця відгуків) виявились "
            f"недоступні в цьому запуску ({exc}) — показ вакансій без дедублікації."
        )


def _scrape_all(today: date) -> tuple[list[RawJobPosting], dict[str, SourceStatus], dict[str, str]]:
    all_jobs: list[RawJobPosting] = []
    statuses: dict[str, SourceStatus] = {}
    collection_methods: dict[str, str] = {}
    for source_name, module in SCRAPER_MODULES.items():
        try:
            jobs = list(module.scrape())
            all_jobs.extend(jobs)
            statuses[source_name] = SourceStatus(source=source_name, status="OK")
            collection_methods[source_name] = getattr(module, "COLLECTION_METHOD", "не_встановлено")
            logger.info("%s: зібрано %d кандидатів", source_name, len(jobs))
        except ScraperError as exc:
            statuses[source_name] = SourceStatus(
                source=source_name, status="недоступне", note=str(exc)
            )
            collection_methods[source_name] = "не_встановлено"
            logger.warning("%s недоступне: %s", source_name, exc)
    return all_jobs, statuses, collection_methods


def run_step1(utc_today: date | None = None, local_today: date | None = None) -> dict:
    utc_today = utc_today or datetime.now(timezone.utc).date()
    local_today = local_today or datetime.now(ZoneInfo(CANDIDATE_LOCAL_TZ)).date()
    local_yesterday = local_today - timedelta(days=1)

    raw_jobs, source_statuses, collection_methods = _scrape_all(utc_today)
    total_found_before_filters = len(raw_jobs)

    empty_result = {
        "vacancies": [],
        "total_found_before_filters": total_found_before_filters,
        "source_statuses": source_statuses,
        "collection_methods": collection_methods,
        "dedup_log_updated": False,
        "dedup_log_note": "",
        "dedup_level_used": 0,
    }
    if not raw_jobs:
        return empty_result

    # Claude оцінює всі зібрані кандидати одним викликом (батч) — за потреби
    # можна розбити на шматки по ~40 вакансій, якщо контекст завеликий.
    CHUNK = 40
    evaluations: list[dict] = []
    for start in range(0, len(raw_jobs), CHUNK):
        chunk = raw_jobs[start : start + CHUNK]
        prompt = build_vacancy_eval_prompt(chunk, today=local_today)
        result = call_json(prompt)
        for ev in result.get("evaluations", []):
            ev["raw_index"] += start  # зсув індексів під повний список
        evaluations.extend(result.get("evaluations", []))

    eval_by_index = {ev["raw_index"]: ev for ev in evaluations}

    # --- Дедублікація: Рівень 1 (лог), з відкатом на Рівень 2 (таблиця відгуків) ---
    dedup_doc_id, log_text = _read_dedup_log_with_retry()
    dedup_level_used = 0
    dedup_log_note = ""
    log_entries: list[DedupEntry] = []
    tracker_urls: set[str] | None = None

    if log_text is not None:
        dedup_level_used = 1
        log_entries = _parse_dedup_log(log_text)
    else:
        tracker_urls, dedup_log_note = _level2_tracker_urls()
        dedup_level_used = 2 if tracker_urls is not None else 0

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
            if parsed and parsed not in (local_today, local_yesterday):
                continue  # поза вікном "останні 2 дні" (місцевий час кандидата) — не показуємо

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

        if dedup_level_used == 1:
            if _is_duplicate(vacancy.dedup_key(), log_entries):
                continue
        elif dedup_level_used == 2:
            if vacancy.url and vacancy.url in (tracker_urls or set()):
                continue
        # dedup_level_used == 0: обидва рівні недоступні — показуємо без дедублікації.

        scored.append(vacancy)

    scored.sort(key=lambda v: MATCH_ORDER.get(v.match_level, 3))

    # Оновлення лога (тільки якщо Рівень 1 фактично читався — інакше
    # перезаписувати лог тим, чого не читали, ризиковано: могли б стерти
    # рядки, що просто не вдалось прочитати в ЦЬОМУ запуску).
    dedup_log_updated = False
    if dedup_level_used == 1 and dedup_doc_id:
        try:
            cutoff = utc_today - timedelta(days=DEDUP_LOG_MAX_AGE_DAYS)
            kept_lines = []
            for entry in log_entries:
                try:
                    entry_date = datetime.fromisoformat(entry.shown_date).date()
                except ValueError:
                    continue
                if entry_date >= cutoff:
                    kept_lines.append(f"{entry.shown_date} | {entry.identifier} | {entry.label}")

            new_lines = [
                f"{utc_today.isoformat()} | {v.url or f'(без прямого URL, {v.source})'} | {v.title} — {v.company}"
                for v in scored
            ]

            header = (
                "Лог показаних вакансій — службовий файл автоматичного пайплайна. "
                "Формат рядка: дата_показу (UTC) | URL_або_інший_ідентифікатор | назва посади — компанія.\n"
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
        "collection_methods": collection_methods,
        "dedup_log_updated": dedup_log_updated,
        "dedup_log_note": dedup_log_note,
        "dedup_level_used": dedup_level_used,
    }

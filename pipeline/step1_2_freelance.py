"""Крок 1.2 — фріланс-проєкти (Freelancehunt: 2 категорії + Telegram-канал).

Виконується ЗАВЖДИ одразу після Кроку 1, незалежно від його результату
(main.py, той самий принцип "наступний крок не залежить від успіху
попереднього", що й для Кроку 2). Мета — НЕ пошук роботи за наймом, а
дрібні тренувальні фріланс-проєкти для практики й портфоліо (1-2/тиждень).

Архітектурно дзеркалить pipeline/step1_vacancies.py:
  - скрапінг (окремий модуль на джерело, ScraperError -> "недоступне",
    не зупиняє решту джерел);
  - Claude оцінює лише СЕМАНТИКУ (релевантність, дата) — дедуп, лічильники,
    конкуренція за кількістю ставок рахує Python;
  - дедублікація за URL у службовому Google-документі, той самий log-файл
    для ВСІХ трьох джерел (на відміну від Кроку 1, де кожне джерело просто
    частина одного списку — тут це явно один спільний файл, як і в
    текстовій версії промпту).

Ключова відмінність від Кроку 1: дедуп-лог тут БЕЗ дворівневої страховки
(рівень 2 "Ворошилов відгуки на вакансії" стосується саме вакансій, для
фрілансу окремого рівня 2 в текстовій версії промпту не було) — якщо лог
недоступний навіть після повторної спроби, показуємо без дедублікації з
явним попередженням, так само як і найпростіший випадок Кроку 1.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from claude_orchestrator.client import call_json
from claude_orchestrator.prompts import build_freelance_eval_prompt
from config import (
    CANDIDATE_LOCAL_TZ,
    CLAUDE_EVAL_CHUNK_SIZE,
    FREELANCE_DEDUP_LOG_MAX_AGE_DAYS,
    FREELANCE_DEDUP_LOG_TITLE,
    FREELANCEHUNT_LOW_BIDS_MAX,
    FREELANCEHUNT_MEDIUM_BIDS_MAX,
    FREELANCE_METRICS_SHEET_TITLE,
    FREELANCE_ZERO_STREAK_REVIEW_THRESHOLD,
    RESUME_FOLDER_ID,
)
from google_services import docs, drive, sheets
from models import FREELANCE_METRICS_HEADER, FreelanceRunMetrics, RawFreelanceProject, ScoredFreelanceProject, SourceStatus
from scrapers import FREELANCE_SCRAPER_MODULES
from scrapers.base import ScraperError

logger = logging.getLogger(__name__)


@dataclass
class FreelanceDedupEntry:
    shown_date: str
    url: str
    label: str  # "назва проєкту — категорія"


def _competition_level(bids_count: int | None) -> str | None:
    if bids_count is None:
        return None  # Telegram-канал: позначку конкуренції не виводимо (немає даних про ставки)
    if bids_count <= FREELANCEHUNT_LOW_BIDS_MAX:
        return "низька"
    if bids_count <= FREELANCEHUNT_MEDIUM_BIDS_MAX:
        return "середня"
    return "висока"


def _parse_dedup_log(text: str) -> list[FreelanceDedupEntry]:
    entries: list[FreelanceDedupEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        # maxsplit=2 — та сама причина, що й у step1_vacancies.py::_parse_dedup_log:
        # назва проєкту (parts[2]) може містити "|", і без обмеження split()
        # мовчки обрізав би label на першому зайвому символі.
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) < 3:
            continue
        entries.append(FreelanceDedupEntry(shown_date=parts[0], url=parts[1], label=parts[2]))
    return entries


_DEDUP_HEADER = (
    "Лог показаних фріланс-проєктів — службовий файл автоматичного пайплайна "
    "(Крок 1.2: Freelancehunt BI/SQL-категорії + Telegram-канал). Формат "
    "рядка: дата_показу (UTC) | URL | назва проєкту — категорія.\n"
)


def _get_or_create_dedup_doc() -> str:
    existing = drive.find_file_by_title(FREELANCE_DEDUP_LOG_TITLE, RESUME_FOLDER_ID)
    if existing:
        return existing["id"]
    doc_id = drive.create_google_doc(FREELANCE_DEDUP_LOG_TITLE, RESUME_FOLDER_ID)
    docs.replace_full_text(doc_id, _DEDUP_HEADER)
    return doc_id


def _read_dedup_log_with_retry() -> tuple[str | None, str | None]:
    doc_id = None
    for attempt in range(2):
        try:
            doc_id = _get_or_create_dedup_doc()
            text = docs.read_full_text(doc_id)
            return doc_id, text
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Спроба %d читання лога дублів фрілансу провалилась: %s", attempt + 1, exc
            )
    return doc_id, None


def _scrape_all() -> tuple[list[RawFreelanceProject], dict[str, SourceStatus]]:
    all_projects: list[RawFreelanceProject] = []
    statuses: dict[str, SourceStatus] = {}
    for source_name, module in FREELANCE_SCRAPER_MODULES.items():
        try:
            projects = list(module.scrape())
            all_projects.extend(projects)
            statuses[source_name] = SourceStatus(source=source_name, status="OK")
            logger.info("%s: зібрано %d кандидатів", source_name, len(projects))
        except ScraperError as exc:
            statuses[source_name] = SourceStatus(source=source_name, status="недоступне", note=str(exc))
            logger.warning("%s недоступне: %s", source_name, exc)
    return all_projects, statuses


def _get_or_create_metrics_sheet() -> str:
    existing = drive.find_file_by_title(FREELANCE_METRICS_SHEET_TITLE, RESUME_FOLDER_ID)
    if existing:
        return existing["id"]
    sheet_id = drive.create_spreadsheet(FREELANCE_METRICS_SHEET_TITLE, RESUME_FOLDER_ID)
    sheets.ensure_header(sheet_id, header=FREELANCE_METRICS_HEADER)
    return sheet_id


def _previous_streaks(sheet_id: str) -> tuple[int, int, int]:
    """Читає останній рядок метрик, повертає (streak_bi, streak_sql,
    streak_telegram) ЯКІ БУЛИ до цього запуску — базове значення для
    інкременту/скидання нижче. (0, 0, 0), якщо історії ще немає."""
    try:
        rows = sheets.read_last_data_rows(sheet_id, 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не вдалось прочитати попередній рядок метрик фрілансу: %s", exc)
        return 0, 0, 0
    if not rows:
        return 0, 0, 0
    row = rows[0]
    idx_bi = FREELANCE_METRICS_HEADER.index("Дні поспіль з 0 (BI и аналитика данных)")
    idx_sql = FREELANCE_METRICS_HEADER.index("Дні поспіль з 0 (Базы данных и SQL)")
    idx_tg = FREELANCE_METRICS_HEADER.index("Дні поспіль з 0 (Telegram-канал)")

    def _int_or_zero(row: list[str], idx: int) -> int:
        if idx >= len(row):
            return 0
        try:
            return int(row[idx])
        except (ValueError, TypeError):
            return 0

    return _int_or_zero(row, idx_bi), _int_or_zero(row, idx_sql), _int_or_zero(row, idx_tg)


def run_step1_2(utc_today: date | None = None, local_today: date | None = None) -> dict:
    utc_today = utc_today or datetime.now(timezone.utc).date()
    local_today = local_today or datetime.now(ZoneInfo(CANDIDATE_LOCAL_TZ)).date()
    local_yesterday = local_today - timedelta(days=1)

    raw_projects, source_statuses = _scrape_all()

    empty_result = {
        "projects": [],
        "source_statuses": source_statuses,
        "dedup_log_updated": False,
        "dedup_log_note": "",
        "metrics_saved": False,
        "metrics_error": "",
        "reviewed_by_category": {"BI и аналитика данных": 0, "Базы данных и SQL": 0, "telegram": 0},
        "rejected_irrelevant": 0,
    }
    if not raw_projects:
        # Усі джерела недоступні або порожні — все одно пишемо метрику
        # нижче (0/0/0), не рано-виходимо, як і Крок 1 при total=0 не
        # намагається зберегти метрику через порожній результат.
        pass

    reviewed_by_category = {"BI и аналитика данных": 0, "Базы данных и SQL": 0, "telegram": 0}
    for p in raw_projects:
        key = "telegram" if p.source == "telegram" else p.category
        reviewed_by_category[key] = reviewed_by_category.get(key, 0) + 1

    # Лог дублів читається ДО оцінки Claude (не після, як раніше) — той
    # самий принцип, що й у step1_vacancies.py::run_step1: URL, уже
    # присутній у лозі, і так буде відкинутий нижче, тож немає сенсу
    # платити за оцінку Claude наперед.
    dedup_doc_id, log_text = _read_dedup_log_with_retry()
    dedup_available = log_text is not None
    dedup_log_note = ""
    log_entries: list[FreelanceDedupEntry] = []
    if dedup_available:
        log_entries = _parse_dedup_log(log_text)
    else:
        dedup_log_note = (
            "Лог дублів фрілансу недоступний навіть після повторної спроби — "
            "показ без дедублікації в цьому запуску."
        )
    seen_log_urls = {e.url for e in log_entries}

    # Пре-фільтр за URL ДО оцінки Claude — на відміну від вакансій дедуп-ключ
    # фрілансу завжди `url` (ScoredFreelanceProject.dedup_key), ніколи
    # нормалізований title/company, тому тут фільтр повний, без застережень
    # про "лише URL-кейс": усе, що відфільтровано тут, гарантовано було б
    # відкинуто і пост-оцінковою перевіркою нижче. Помітний побічний ефект:
    # проєкт, який одночасно і вже показаний, і був би оцінений як
    # нерелевантний, тепер НЕ потрапляє в rejected_irrelevant (раніше
    # потрапляв, бо оцінювався раніше дедуп-перевірки) — вважаю це
    # правильнішим: лічильник "відхилено як нерелевантні" мав би описувати
    # НОВІ рішення цього запуску, а не вже відомі дублікати.
    projects_to_evaluate = [p for p in raw_projects if not (dedup_available and p.url in seen_log_urls)]
    skipped_known_url_count = len(raw_projects) - len(projects_to_evaluate)
    if skipped_known_url_count:
        logger.info(
            "%d фріланс-проєктів відфільтровано за відомим URL ДО оцінки Claude (економія викликів)",
            skipped_known_url_count,
        )

    evaluations: list[dict] = []
    if projects_to_evaluate:
        for start in range(0, len(projects_to_evaluate), CLAUDE_EVAL_CHUNK_SIZE):
            chunk = projects_to_evaluate[start : start + CLAUDE_EVAL_CHUNK_SIZE]
            prompt = build_freelance_eval_prompt(chunk, today=local_today)
            result = call_json(prompt)
            for ev in result.get("evaluations", []):
                ev["raw_index"] += start
            evaluations.extend(result.get("evaluations", []))
    eval_by_index = {ev["raw_index"]: ev for ev in evaluations}

    scored: list[ScoredFreelanceProject] = []
    rejected_irrelevant = 0

    for i, project in enumerate(projects_to_evaluate):
        ev = eval_by_index.get(i)
        if not ev or not ev.get("is_relevant"):
            rejected_irrelevant += 1
            continue

        posted_date = ev.get("posted_date")
        date_undetermined = bool(ev.get("date_undetermined"))
        if not date_undetermined and posted_date:
            try:
                parsed = datetime.fromisoformat(posted_date).date()
            except ValueError:
                parsed = None
            if parsed and parsed not in (local_today, local_yesterday):
                continue  # поза вікном свіжості 1-2 дні

        # (Пост-оцінкової дедуп-перевірки тут свідомо немає: на відміну від
        # вакансій, dedup_key фрілансу — завжди project.url, ніколи
        # нормалізований title/company, тож пре-фільтр вище вже виключив із
        # projects_to_evaluate все, що сюди могло б потрапити.)

        scored.append(
            ScoredFreelanceProject(
                title=project.title,
                source=project.source,
                category=project.category,
                url=project.url,
                why_relevant=ev.get("why_relevant") or "",
                posted_date=posted_date,
                bids_count=project.bids_count,
                competition_level=_competition_level(project.bids_count),
                budget_raw=project.budget_raw or "не вказано",
            )
        )

    dedup_log_updated = False
    if dedup_available and dedup_doc_id:
        try:
            cutoff = utc_today - timedelta(days=FREELANCE_DEDUP_LOG_MAX_AGE_DAYS)
            kept_lines = []
            for entry in log_entries:
                try:
                    entry_date = datetime.fromisoformat(entry.shown_date).date()
                except ValueError:
                    continue
                if entry_date >= cutoff:
                    kept_lines.append(f"{entry.shown_date} | {entry.url} | {entry.label}")
            new_lines = [
                f"{utc_today.isoformat()} | {v.url} | {v.title} — {v.category}" for v in scored
            ]
            full_content = _DEDUP_HEADER + "\n".join(kept_lines + new_lines) + (
                "\n" if kept_lines or new_lines else ""
            )
            docs.replace_full_text(dedup_doc_id, full_content)
            dedup_log_updated = True
        except Exception as exc:  # noqa: BLE001
            dedup_log_note = f"лог дублів фрілансу не вдалось оновити: {exc}"
            logger.exception("Не вдалось оновити лог дублів фрілансу")

    # --- Метрики ---
    shown_bi = sum(1 for v in scored if v.category == "BI и аналитика данных")
    shown_sql = sum(1 for v in scored if v.category == "Базы данных и SQL")
    shown_telegram = sum(1 for v in scored if v.source == "telegram")

    status_bi = source_statuses.get("freelancehunt.com", SourceStatus("freelancehunt.com")).status
    status_sql = status_bi  # один модуль на обидві категорії — статус спільний
    status_telegram = source_statuses.get("telegram", SourceStatus("telegram")).status

    metrics_saved = False
    metrics_error = ""
    zero_streak_notes: list[str] = []
    try:
        sheet_id = _get_or_create_metrics_sheet()
        sheets.ensure_header(sheet_id, header=FREELANCE_METRICS_HEADER)
        prev_bi, prev_sql, prev_tg = _previous_streaks(sheet_id)

        def _next_streak(prev: int, shown: int) -> int:
            return 0 if shown > 0 else prev + 1

        streak_bi = _next_streak(prev_bi, shown_bi)
        streak_sql = _next_streak(prev_sql, shown_sql)
        streak_tg = _next_streak(prev_tg, shown_telegram)

        for label, streak in (
            ("BI и аналитика данных", streak_bi),
            ("Базы данных и SQL", streak_sql),
            ("Telegram-канал", streak_tg),
        ):
            if streak == FREELANCE_ZERO_STREAK_REVIEW_THRESHOLD:
                zero_streak_notes.append(
                    f"{label}: {streak} днів поспіль без релевантних результатів — "
                    "варто переглянути разом із Володимиром (джерело малоактивне чи "
                    "критерії пошуку застарілі)."
                )

        notes_parts = [dedup_log_note] if dedup_log_note else []
        notes_parts.extend(zero_streak_notes)

        metrics = FreelanceRunMetrics(
            run_date=utc_today.isoformat(),
            shown_bi=shown_bi,
            shown_sql=shown_sql,
            shown_total=shown_bi + shown_sql + shown_telegram,
            rejected_irrelevant=rejected_irrelevant,
            status_bi=status_bi,
            status_sql=status_sql,
            streak_zero_bi=streak_bi,
            streak_zero_sql=streak_sql,
            notes="; ".join(notes_parts),
            shown_telegram=shown_telegram,
            status_telegram=status_telegram,
            streak_zero_telegram=streak_tg,
        )
        sheets.append_row(sheet_id, metrics.as_row())
        metrics_saved = True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не вдалось зберегти метрику фрілансу")
        metrics_error = str(exc)

    return {
        "projects": scored,
        "source_statuses": source_statuses,
        "dedup_log_updated": dedup_log_updated,
        "dedup_log_note": dedup_log_note,
        "metrics_saved": metrics_saved,
        "metrics_error": metrics_error,
        "reviewed_by_category": reviewed_by_category,
        "rejected_irrelevant": rejected_irrelevant,
    }

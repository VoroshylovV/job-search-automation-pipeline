"""Крок 1 — нові вакансії.

Оркеструє: скрапінг 5 джерел (з фільтрами remote/досвід у запиті) ->
детермінований стоп за рівнем у назві -> етап 1: Claude по картці (грубе
сито) -> етап 2: повний текст вакансії + вирішальна оцінка Claude
(критерії/Match-рівень/ветерани/локація/дата; формат має бути явно
підтверджений текстом) -> дедуплікація (детерміновано, в Python; обидва рівні —
лог показаних і таблиця відгуків — виконуються паралельно, за ID вакансії) -> сортування за Match-рівнем -> оновлення лога
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
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from claude_orchestrator.client import ClaudeTruncatedError, call_json
from claude_orchestrator.prompts import build_vacancy_eval_prompt
from config import (
    CANDIDATE_LOCAL_TZ,
    CLAUDE_EVAL_CHUNK_SIZE,
    CLAUDE_EVAL_MAX_TOKENS,
    CLAUDE_FULL_EVAL_CHUNK_SIZE,
    DATE_WINDOW_DAYS,
    DEDUP_LOG_MAX_AGE_DAYS,
    DEDUP_LOG_TITLE,
    RESUME_FOLDER_ID,
    TRACKER_COMPANY_COLUMN_HEADER,
    TRACKER_DATE_COLUMN_HEADER,
    TRACKER_RESULT_COLUMN_HEADER,
    TRACKER_SHEET_TITLE,
    TRACKER_URL_COLUMN_HEADER,
    SENIORITY_STOP_PATTERN,
    UNKNOWN_DATE_FALLBACK_LIMIT,
)
from google_services import docs, drive, sheets
from models import RawJobPosting, ScoredVacancy, SourceStatus
from scrapers import SCRAPER_MODULES
from scrapers.base import ScraperError
from scrapers.detail import fetch_full_text

logger = logging.getLogger(__name__)

MATCH_ORDER = {"High": 0, "Medium": 1, "Low": 2}
_SENIORITY_RE = re.compile(SENIORITY_STOP_PATTERN, re.IGNORECASE)


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
        # maxsplit=2: назва посади/компанії (parts[2]) може легітимно містити
        # "|" (наприклад, у назві проєкту чи описі) — без обмеження split()
        # мовчки обрізав би label на першому зайвому "|", і для вакансій без
        # прямого URL (де dedup_key будується з повного "title|company")
        # обрізаний label більше не збігався б при порівнянні в _is_duplicate_in_log.
        parts = [p.strip() for p in line.split("|", 2)]
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


def _norm_url(url: str) -> str:
    """Порівняння URL без різниці http/https, www і фінального слеша."""
    u = url.strip().lower().split("#")[0]
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.rstrip("/")


_ID_PATTERNS = [re.compile(p) for p in (r"vacancy(\d+)", r"[?&]id=(\d+)", r"/vacancies/(\d+)", r"/jobs/(\d+)")]


def vacancy_key(url: str | None) -> str | None:
    r"""Ключ дедублікації за ID вакансії, видобутим з URL (`vacancy\d+`,
    `id=\d+`, `/vacancies/\d+`, `/jobs/\d+`) разом із доменом, а не за
    збігом повного рядка URL. None, якщо ID видобути не вдається — тоді
    звірка йде за парою «компанія + назва посади»."""
    if not url:
        return None
    norm = _norm_url(url)
    for pat in _ID_PATTERNS:
        m = pat.search(norm)
        if m:
            domain = norm.split("/")[0].split("?")[0]
            domain = ".".join(domain.split(".")[-2:])
            return f"{domain}:{m.group(1)}"
    return None


def _same_vacancy_url(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    ka, kb = vacancy_key(a), vacancy_key(b)
    return (ka is not None and ka == kb) or _norm_url(a) == _norm_url(b)


def _norm_name(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


@dataclass
class TrackerRow:
    url: str
    company: str
    date: str
    status: str


def _read_tracker_rows() -> list[TrackerRow] | None:
    """Рівень 2 дедублікації: рядки таблиці «Ворошилов відгуки на вакансії»
    (URL, компанія, дата відгуку, результат). None — таблиця недоступна. Читається
    ЗАВЖДИ, паралельно з логом показаних (Рівень 1), а не лише як фолбек."""
    try:
        tracker = drive.find_file_by_title(TRACKER_SHEET_TITLE, RESUME_FOLDER_ID)
        if not tracker:
            return None
        rows = sheets.read_rows_by_headers(tracker["id"], [
            TRACKER_URL_COLUMN_HEADER, TRACKER_COMPANY_COLUMN_HEADER,
            TRACKER_DATE_COLUMN_HEADER, TRACKER_RESULT_COLUMN_HEADER,
        ])
        return [
            TrackerRow(
                url=r.get(TRACKER_URL_COLUMN_HEADER, ""),
                company=r.get(TRACKER_COMPANY_COLUMN_HEADER, ""),
                date=r.get(TRACKER_DATE_COLUMN_HEADER, ""),
                status=r.get(TRACKER_RESULT_COLUMN_HEADER, ""),
            )
            for r in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Рівень 2 дедублікації (таблиця відгуків) недоступний: %s", exc)
        return None


def _is_duplicate_in_log(vacancy: ScoredVacancy, log_entries: list[DedupEntry]) -> bool:
    key = vacancy_key(vacancy.url)
    label = f"{vacancy.title} — {vacancy.company}".lower()
    for entry in log_entries:
        if _same_vacancy_url(vacancy.url, entry.identifier):
            return True
        if key is None and (entry.identifier == vacancy.dedup_key() or entry.label.lower() == label):
            return True  # ID з URL не видобути — звірка за «компанія + назва»
    return False


def _parse_date(text: str | None) -> date | None:
    t = (text or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _tracker_check(vacancy: ScoredVacancy, rows: list[TrackerRow]) -> tuple[str, str]:
    """('дубль', status) — вакансію вже подано; ('прапорець', status) — компанія
    є в таблиці під іншою вакансією (показуємо з позначкою); ('', '') — ні.
    Дубль, якщо: (а) збігся ID вакансії з URL; (б) ID видобути не вдається
    (у вакансії або в рядку таблиці) і збігаються компанія + дата; (в) для тієї
    самої компанії URL у таблиці порожній. За назвою посади НЕ звіряємо —
    у таблиці її немає окремою колонкою."""
    flag_status = None
    company = _norm_name(vacancy.company)
    posted = _parse_date(vacancy.posted_date)
    for row in rows:
        if _same_vacancy_url(vacancy.url, row.url):
            return "дубль", row.status
        if company and _norm_name(row.company) == company:
            if not row.url.strip():
                return "дубль", row.status
            no_id = vacancy_key(vacancy.url) is None or vacancy_key(row.url) is None
            if no_id and posted is not None and posted == _parse_date(row.date):
                return "дубль", row.status
            flag_status = flag_status if flag_status is not None else row.status
    if flag_status is not None:
        return "прапорець", flag_status
    return "", ""


def _final_match_level(ev: dict, card_only: bool) -> str:
    """Match-рівень за формулою A+B+C (по 0-2): High = 5-6 і A>0, B>0;
    Medium = 3-4; Low = 0-2 або A=0 або B=0. Кап моделі (досвід >1.5 року ->
    максимум Medium) лишається чинним: беремо гіршу з двох оцінок. Лише
    картка (повної сторінки немає) — не вище Medium."""
    model_level = ev.get("match_level") if ev.get("match_level") in MATCH_ORDER else None
    scores = ev.get("match_scores")
    level = model_level or "Low"
    if isinstance(scores, dict):
        try:
            a, b, c = (int(scores[k]) for k in ("skills", "duties", "expectations"))
        except (KeyError, TypeError, ValueError):
            a = None
        if a is not None and all(0 <= x <= 2 for x in (a, b, c)):
            total = a + b + c
            computed = "Low" if (a == 0 or b == 0 or total <= 2) else ("High" if total >= 5 else "Medium")
            level = computed if model_level is None else max(computed, model_level, key=MATCH_ORDER.get)
    if card_only and level == "High":
        level = "Medium"
    return level


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


def _decision(job: RawJobPosting, result: str, code: str, detail: str = "",
              posted_date: str | None = None, date_source: str = "") -> dict:
    """Рядок діагностичного логу причин відсіву (див. pipeline/reject_log.py)."""
    return {
        "source": job.source,
        "url": job.url or "",
        "label": f"{job.title} — {job.company}",
        "result": result,
        "code": code,
        "detail": detail,
        "posted_date": posted_date or "",
        "date_source": date_source,
    }


def _date_source(job: RawJobPosting, ev: dict | None, full_text: bool = False) -> str:
    """Коди — спільні з онлайн-версією: «сторінка» (повна сторінка вакансії),
    «картка» (список видачі), «невідома»."""
    if ev and ev.get("posted_date") and not ev.get("date_undetermined"):
        return "сторінка" if full_text else "картка"
    if job.posted_raw:
        return "картка"
    return "невідома"


EVAL_FAILED_RESULT = "не оцінено (помилка Claude)"


def _eval_failed_marker() -> dict:
    """Заглушка оцінки для вакансій, чий батч не вдалось оцінити: не проходить
    критерії, але в лозі відсіву позначається окремо від справжнього відсіву."""
    return {"passes_criteria": False, "reject_code": "інше", "reject_reason": "", "eval_failed": True}


def _evaluate_batch(chunk: list[RawJobPosting], today: date, stage: str,
                    chunk_texts: dict[int, str]) -> dict[int, dict] | None:
    """Оцінка одного батча. Обрізана відповідь (max_tokens) — батч ділиться
    навпіл і кожна половина повторюється; інша помилка Claude → None (вакансії
    батча лишаються неоціненими, решту Кроку 1 це не валить)."""
    prompt = build_vacancy_eval_prompt(chunk, today=today, stage=stage, full_texts=chunk_texts)
    try:
        response = call_json(
            prompt, max_tokens=CLAUDE_EVAL_MAX_TOKENS, stage="fulltext" if stage == "full" else "cards"
        )
    except ClaudeTruncatedError:
        if len(chunk) == 1:
            logger.error("Відповідь Claude обрізана навіть для однієї вакансії — не оцінено")
            return None
        mid = len(chunk) // 2
        logger.warning("Відповідь Claude обрізана (%d вакансій) — ділю батч навпіл", len(chunk))
        out: dict[int, dict] = {}
        for lo, hi in ((0, mid), (mid, len(chunk))):
            part_texts = {i - lo: t for i, t in chunk_texts.items() if lo <= i < hi}
            part = _evaluate_batch(chunk[lo:hi], today, stage, part_texts)
            if part is None:
                out.update({i: _eval_failed_marker() for i in range(lo, hi)})
            else:
                out.update({i + lo: ev for i, ev in part.items()})
        return out
    except Exception as exc:  # noqa: BLE001 - будь-яка помилка API/JSON не має валити Крок 1
        logger.error("Батч з %d вакансій не оцінено (помилка Claude): %s", len(chunk), exc)
        return None
    result: dict[int, dict] = {}
    for ev in response.get("evaluations", []):
        idx = ev.get("raw_index")
        if isinstance(idx, int) and 0 <= idx < len(chunk):
            result[idx] = ev
    return result


def _evaluate(jobs: list[RawJobPosting], today: date, stage: str,
              full_texts: dict[int, str] | None = None) -> dict[int, dict]:
    """Оцінка Claude батчами (CLAUDE_EVAL_CHUNK_SIZE для картки,
    CLAUDE_FULL_EVAL_CHUNK_SIZE для повних текстів). Повертає
    {індекс у jobs: evaluation}; для вакансій батча, що не оцінився, —
    маркер `eval_failed` (див. _eval_failed_marker)."""
    chunk_size = CLAUDE_FULL_EVAL_CHUNK_SIZE if stage == "full" else CLAUDE_EVAL_CHUNK_SIZE
    full_texts = full_texts or {}
    result: dict[int, dict] = {}
    for start in range(0, len(jobs), chunk_size):
        chunk = jobs[start : start + chunk_size]
        chunk_texts = {i - start: t for i, t in full_texts.items() if start <= i < start + len(chunk)}
        batch = _evaluate_batch(chunk, today, stage, chunk_texts)
        if batch is None:
            result.update({start + i: _eval_failed_marker() for i in range(len(chunk))})
        else:
            result.update({start + i: ev for i, ev in batch.items()})
    return result


def _confirm_with_full_text(jobs: list[RawJobPosting], today: date,
                            stage1_evals: list[dict] | None = None) -> tuple[dict[int, dict], set[int]]:
    """Етап 2: повний текст кожної вакансії, що пройшла картку, і
    вирішальна оцінка за ним. Повертає ({індекс: evaluation}, множина
    індексів, для яких повний текст справді отримано). stage1_evals —
    оцінки етапу 1 у тому самому порядку (тут не використовуються; потрібні
    тестам, які підміняють етап 2 «прозорим» без мережі)."""
    full_texts: dict[int, str] = {}
    for i, job in enumerate(jobs):
        text = fetch_full_text(job)
        if text:
            full_texts[i] = text
    logger.info("Етап 2: повний текст отримано для %d з %d вакансій", len(full_texts), len(jobs))
    if not jobs:
        return {}, set()
    return _evaluate(jobs, today, "full", full_texts), set(full_texts)


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
        "unknown_date_note": "",
        "decisions": [],
        "unevaluated_count": 0,
        "evaluated_count": 0,
    }
    if not raw_jobs:
        return empty_result

    # --- Дедублікація: Рівень 1 (лог показаних) і Рівень 2 (таблиця
    # відгуків) виконуються ОБОВ'ЯЗКОВО й ПАРАЛЕЛЬНО, незалежно один від
    # одного (не лише як фолбек). Читаються ДО оцінки Claude, щоб відомі за
    # ID вакансії відфільтрувати ще до платного виклику API.
    dedup_doc_id, log_text = _read_dedup_log_with_retry()
    tracker_rows = _read_tracker_rows()
    log_entries: list[DedupEntry] = _parse_dedup_log(log_text) if log_text is not None else []
    if log_text is not None:
        dedup_level_used = 1
        dedup_log_note = (
            "" if tracker_rows is not None
            else "Таблиця відгуків недоступна в цьому запуску — дедублікація лише за логом показаних."
        )
    elif tracker_rows is not None:
        dedup_level_used = 2
        dedup_log_note = (
            "Лог дублів недоступний; дедублікацію виконано по таблиці відгуків "
            "(охоплює лише вакансії, на які подано, — показані-але-не-подані могли пройти повторно)."
        )
    else:
        dedup_level_used = 0
        dedup_log_note = (
            "Обидва рівні дедублікації (лог і таблиця відгуків) виявились "
            "недоступні в цьому запуску — показ вакансій без дедублікації."
        )

    # Пре-фільтр за ID вакансії ДО оцінки Claude. НЕ замінює пост-оцінкову
    # перевірку нижче (та лишається обов'язковою): для вакансій БЕЗ ID/URL
    # ключ будується з нормалізованих title/company, які повертає Claude.
    log_urls = [e.identifier for e in log_entries]
    applied_urls = [r.url for r in (tracker_rows or []) if r.url.strip()]

    decisions: list[dict] = []
    jobs_to_evaluate = []
    for j in raw_jobs:
        applied = j.url and any(_same_vacancy_url(j.url, u) for u in applied_urls)
        if j.url and (applied or any(_same_vacancy_url(j.url, u) for u in log_urls)):
            code = "подано" if applied else "дубль"
            decisions.append(_decision(j, "відсіяно", code, "відомий ID вакансії до оцінки", date_source=_date_source(j, None)))
        else:
            jobs_to_evaluate.append(j)
    skipped_known_url_count = len(raw_jobs) - len(jobs_to_evaluate)
    if skipped_known_url_count:
        logger.info(
            "%d вакансій відфільтровано за відомим ID вакансії ДО оцінки Claude (економія викликів)",
            skipped_known_url_count,
        )

    # Claude оцінює лише те, що не відфільтровано вище, одним викликом на
    # шматок (config.CLAUDE_EVAL_CHUNK_SIZE) — за потреби зменш його там,
    # якщо контекст завеликий.
    # Етап 0 (детерміновано, без Claude): явний рівень вище Junior у назві
    # і вакансії без URL (у звіті вакансія без посилання невалідна — Правило
    # В онлайн-промпту від 02.10.2026).
    remaining: list[RawJobPosting] = []
    for j in jobs_to_evaluate:
        m = _SENIORITY_RE.search(j.title or "")
        if m:
            decisions.append(_decision(j, "відсіяно", "досвід", f"рівень у назві: {m.group(0)}", date_source=_date_source(j, None)))
        elif not j.url:
            decisions.append(_decision(j, "відсіяно", "інше", "немає URL вакансії", date_source=_date_source(j, None)))
        else:
            remaining.append(j)
    jobs_to_evaluate = remaining

    # Етап 1: картка (грубе сито). Етап 2: повний текст — вирішальний.
    eval_by_index = _evaluate(jobs_to_evaluate, local_today, "card")
    # «Оцінено» для журналу вартості: вакансії, що реально отримали оцінку етапу 1.
    evaluated_count = sum(1 for ev in eval_by_index.values() if not ev.get("eval_failed"))
    stage1_passed = [i for i in range(len(jobs_to_evaluate))
                     if eval_by_index.get(i) and eval_by_index[i].get("passes_criteria")]
    stage2_evals, full_text_ok = _confirm_with_full_text(
        [jobs_to_evaluate[i] for i in stage1_passed], local_today,
        [eval_by_index[i] for i in stage1_passed],
    )
    full_text_indices: set[int] = set()
    for k, i in enumerate(stage1_passed):
        ev2 = stage2_evals.get(k)
        if ev2 is None:
            ev2 = {"passes_criteria": False, "reject_code": "інше", "reject_reason": "етап 2 без оцінки"}
        elif ev2.get("eval_failed"):
            ev2 = {**ev2, "stage": 2}
        eval_by_index[i] = ev2
        if k in full_text_ok:
            full_text_indices.add(i)

    scored: list[ScoredVacancy] = []
    # Дату не завжди вдається розпізнати (ні скрапер, ні Claude не витягли
    # жодної вказівки з posted_raw/description_snippet) — вікно "сьогодні/
    # вчора" тоді просто не застосовне, і вакансія свідомо НЕ відкидається
    # (могла бути свіжою). Але без обмеження це дірка у фільтрі "останні 2
    # дні": ліміт нижче — свідоме рішення, скільки таких вакансій пускати
    # в звіт за один запуск, а не забутий рудимент чат-версії.
    unknown_date_shown = 0
    unevaluated_count = 0
    unknown_date_limit_hit = False
    for i, job in enumerate(jobs_to_evaluate):
        ev = eval_by_index.get(i)
        if not ev:
            decisions.append(_decision(job, "відсіяно", "інше", "модель не повернула оцінку", date_source=_date_source(job, None)))
            continue
        if ev.get("eval_failed"):
            unevaluated_count += 1
            decisions.append(_decision(
                job, EVAL_FAILED_RESULT, "помилка_Claude",
                "етап 2" if ev.get("stage") == 2 else "етап 1", date_source=_date_source(job, None),
            ))
            continue
        if not ev.get("passes_criteria"):
            decisions.append(_decision(
                job, "відсіяно", ev.get("reject_code") or "інше", ev.get("reject_reason") or "",
                ev.get("posted_date"), _date_source(job, ev, i in full_text_indices),
            ))
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
                decisions.append(_decision(job, "відсіяно", "дата", "поза вікном сьогодні/вчора", posted_date, _date_source(job, ev, i in full_text_indices)))
                continue  # поза вікном "останні 2 дні" (місцевий час кандидата) — не показуємо

        if date_undetermined:
            if unknown_date_shown >= UNKNOWN_DATE_FALLBACK_LIMIT:
                unknown_date_limit_hit = True
                decisions.append(_decision(job, "відсіяно", "ліміт_невизначеної_дати", "", None, "невідома"))
                continue  # дата невідома, і ліміт показу таких вакансій за цей запуск вичерпано
            unknown_date_shown += 1

        card_only = i not in full_text_indices
        vacancy = ScoredVacancy(
            title=ev.get("normalized_title") or job.title,
            company=ev.get("normalized_company") or job.company,
            source=job.source,
            url=job.url,
            match_level=_final_match_level(ev, card_only),
            match_reasoning=ev.get("match_reasoning") or "",
            veteran_bonus=bool(ev.get("veteran_bonus")),
            low_match_location=bool(ev.get("low_match_location")),
            low_match_location_reason=ev.get("low_match_location_reason") or "",
            posted_date=posted_date,
            date_undetermined=date_undetermined,
            card_only=card_only,
        )

        # Обидва рівні — паралельно й незалежно (кожен, що доступний).
        if log_text is not None and _is_duplicate_in_log(vacancy, log_entries):
            decisions.append(_decision(job, "відсіяно", "дубль", "після оцінки", posted_date, _date_source(job, ev, i in full_text_indices)))
            continue
        if tracker_rows is not None:
            verdict, status = _tracker_check(vacancy, tracker_rows)
            if verdict == "дубль":
                decisions.append(_decision(job, "відсіяно", "подано", "таблиця відгуків", posted_date, _date_source(job, ev, i in full_text_indices)))
                continue
            if verdict == "прапорець":
                vacancy.company_flag = f"Компанія вже в таблиці: {status or 'без статусу'}"
        # dedup_level_used == 0: обидва рівні недоступні — показуємо без дедублікації.

        scored.append(vacancy)
        decisions.append(_decision(
            job, "показано", "—",
            f"{vacancy.match_level} · {'лише картка' if card_only else 'сторінка'}",
            posted_date, "невідома" if date_undetermined else _date_source(job, ev, i in full_text_indices),
        ))

    scored.sort(key=lambda v: MATCH_ORDER.get(v.match_level, 3))

    # Оновлення лога (тільки якщо Рівень 1 фактично читався — інакше
    # перезаписувати лог тим, чого не читали, ризиковано: могли б стерти
    # рядки, що просто не вдалось прочитати в ЦЬОМУ запуску).
    dedup_log_updated = False
    if log_text is not None and dedup_doc_id:
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

    unknown_date_note = (
        f"Ліміт показу вакансій з невизначеною датою публікації вичерпано "
        f"(UNKNOWN_DATE_FALLBACK_LIMIT={UNKNOWN_DATE_FALLBACK_LIMIT} за цей запуск) — "
        "частину відфільтровано, хоча за іншими критеріями вони пройшли."
        if unknown_date_limit_hit
        else ""
    )

    return {
        "vacancies": scored,
        "total_found_before_filters": total_found_before_filters,
        "source_statuses": source_statuses,
        "collection_methods": collection_methods,
        "dedup_log_updated": dedup_log_updated,
        "dedup_log_note": dedup_log_note,
        "dedup_level_used": dedup_level_used,
        "unknown_date_note": unknown_date_note,
        "decisions": decisions,
        "unevaluated_count": unevaluated_count,
        "evaluated_count": evaluated_count,
    }

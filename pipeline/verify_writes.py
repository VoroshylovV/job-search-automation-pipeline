"""verify_writes() — наприкінці запуску, ОСТАННЬОЮ дією перед фінальним повідомленням. Без викликів моделі.

Перевіряє, що все справді записано:
 1. Лог відсіву за сьогодні (тест гітхаб) існує, modifiedTime — сьогодні (UTC), останній рядок — «ПІДСУМОК»,
    рядків даних ≥ 90% від «знайдено».
 2. У Логу показаних є рядки за сьогодні в кількості «показано» (повторний запуск дня: не менше).
 3. У метриках є рядок за сьогодні із заповненими ключовими колонками (METRICS_KEY_COLUMNS).
 4. Рядок «Результат щоденної перевірки» цього запуску — не «ЗАПУСК ПОЧАТО».
 5. Усі 5 джерел прочитано (жодне не «недоступне»).
Пункт не пройшов — одна спроба дозапису (п.5 — без дозапису: скрейпінг не повторюється) і повторна перевірка.
Знову не вдалось — тривога «🚨🚨🚨 !!! НЕ ЗАПИСАНО: <що саме> !!!» першим рядком (pipeline/alarm.py).
Усе гаразд — у підсумок додається «✅ ВСЕ ЗАПИСАНО І ПЕРЕВІРЕНО»."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime

from config import (
    DEDUP_LOG_TITLE,
    METRICS_KEY_COLUMNS,
    METRICS_SHEET_TITLE,
    REJECT_LOG_FOLDER_ID,
    RESUME_FOLDER_ID,
    SELFCHECK_RUN_STARTED_MARK,
    SELFCHECK_SHEET_TITLE,
    SOURCES,
    VERIFY_MIN_ROWS_SHARE,
)
from google_services import docs, drive, sheets
from models import METRICS_HEADER
from pipeline import alarm, reject_log

logger = logging.getLogger(__name__)

VERIFIED_OK_LINE = "✅ ВСЕ ЗАПИСАНО І ПЕРЕВІРЕНО"


@dataclass
class VerifyContext:
    utc_today: date
    step1_result: dict
    step3_result: dict | None = None
    step4_result: dict | None = None
    selfcheck_timestamp: str = ""
    start_marker: dict | None = None
    reject_log: dict | None = None  # результат reject_log.write_reject_log


@dataclass
class VerifyResult:
    ok: bool
    failed: list[str] = field(default_factory=list)  # «що саме» не записано
    repaired: list[str] = field(default_factory=list)


def _same_date(cell: str, day: date) -> bool:
    cell = (cell or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            if datetime.strptime(cell, fmt).date() == day:
                return True
        except ValueError:
            continue
    return False


# --- перевірки: (ok, що саме не так) ---------------------------------------------
def check_reject_log(ctx: VerifyContext) -> tuple[bool, str]:
    rl = ctx.reject_log
    if not rl:
        return False, "лог відсіву за сьогодні (запис не виконувався)"
    f = drive.find_file_by_title(rl["title"], REJECT_LOG_FOLDER_ID)
    if not f:
        return False, f"лог відсіву «{rl['title']}» відсутній у Drive"
    modified = drive.get_modified_time(f["id"])
    if not _same_date(modified, ctx.utc_today):
        return False, f"лог відсіву: modifiedTime {modified} не сьогодні"
    values = sheets.read_values(f["id"], "A1:ZZ")
    if not values or len(values) < 2 or reject_log.SUMMARY_MARK not in (values[-1][5:6] or [""]):
        return False, "лог відсіву: останній рядок не «ПІДСУМОК»"
    found = ctx.step1_result.get("total_found_before_filters", 0)
    data_rows = len(values) - 2  # без заголовка й підсумку
    if data_rows < math.ceil(found * VERIFY_MIN_ROWS_SHARE):
        return False, f"лог відсіву: рядків {data_rows} з {found} знайдених (< {VERIFY_MIN_ROWS_SHARE:.0%})"
    return True, ""


def _dedup_doc_text() -> str:
    f = drive.find_file_by_title(DEDUP_LOG_TITLE, RESUME_FOLDER_ID)
    if not f:
        raise RuntimeError("лог показаних відсутній у Drive")
    return docs.read_full_text(f["id"])


def check_shown_log(ctx: VerifyContext) -> tuple[bool, str]:
    shown = len(ctx.step1_result.get("vacancies", []))
    today = ctx.utc_today.isoformat()
    try:
        text = _dedup_doc_text()
    except Exception as exc:  # noqa: BLE001
        return False, f"лог показаних вакансій не прочитано ({exc})"
    count = sum(1 for line in text.splitlines() if line.strip().startswith(today) and "|" in line)
    repeat = bool((ctx.reject_log or {}).get("run_index"))
    if count < shown or (count != shown and not repeat):
        return False, f"лог показаних: за сьогодні {count} рядків, показано {shown}"
    return True, ""


def check_metrics(ctx: VerifyContext) -> tuple[bool, str]:
    f = drive.find_file_by_title(METRICS_SHEET_TITLE, RESUME_FOLDER_ID)
    if not f:
        return False, "таблиця метрик відсутня в Drive"
    values = sheets.read_values(f["id"], "A1:ZZ")
    if not values:
        return False, "таблиця метрик порожня"
    header = [h.strip() for h in values[0]]
    todays = [r for r in values[1:] if r and _same_date(r[0], ctx.utc_today)]
    if not todays:
        return False, "метрики: немає рядка за сьогодні"
    row = todays[-1]
    empty = [c for c in METRICS_KEY_COLUMNS if c not in header or header.index(c) >= len(row) or not str(row[header.index(c)]).strip()]
    if empty:
        return False, f"метрики: порожні ключові колонки: {', '.join(empty)}"
    found, shown = ctx.step1_result.get("total_found_before_filters", 0), len(ctx.step1_result.get("vacancies", []))
    if str(row[header.index(METRICS_HEADER[1])]).strip() != str(found) or \
            str(row[header.index(METRICS_HEADER[2])]).strip() != str(shown):
        return False, "метрики: «знайдено»/«показано» не збігаються з результатом запуску"
    return True, ""


def check_selfcheck(ctx: VerifyContext) -> tuple[bool, str]:
    f = drive.find_file_by_title(SELFCHECK_SHEET_TITLE, RESUME_FOLDER_ID)
    if not f:
        return False, "«Результат щоденної перевірки» відсутній у Drive"
    values = sheets.read_values(f["id"], "A1:ZZ")
    ts = ctx.selfcheck_timestamp
    mine = [r for r in values[1:] if r and r[0].strip().startswith(ts)]
    if not mine:
        return False, "«Результат щоденної перевірки»: немає рядка цього запуску"
    if SELFCHECK_RUN_STARTED_MARK in mine[-1][0] or (len(mine[-1]) > 1 and mine[-1][1].strip() == SELFCHECK_RUN_STARTED_MARK):
        return False, f"«Результат щоденної перевірки»: рядок досі «{SELFCHECK_RUN_STARTED_MARK}»"
    return True, ""


def check_sources(ctx: VerifyContext) -> tuple[bool, str]:
    statuses = ctx.step1_result.get("source_statuses") or {}
    bad = [s for s in SOURCES if s not in statuses or statuses[s].status == "недоступне"]
    return (not bad), (f"джерела не прочитано: {', '.join(bad)}" if bad else "")


# --- одна спроба дозапису -----------------------------------------------------------
def repair_reject_log(ctx: VerifyContext) -> None:
    if not ctx.reject_log:
        raise RuntimeError("немає даних логу відсіву для дозапису")
    reject_log.repair_reject_log(ctx.reject_log)


def repair_shown_log(ctx: VerifyContext) -> None:
    """Дописує відсутні рядки цього запуску в Лог показаних."""
    lines = ctx.step1_result.get("shown_log_lines") or []
    f = drive.find_file_by_title(DEDUP_LOG_TITLE, RESUME_FOLDER_ID)
    if not f:
        raise RuntimeError("лог показаних відсутній у Drive")
    text = docs.read_full_text(f["id"])
    existing = {line.strip() for line in text.splitlines()}
    missing = [ln for ln in lines if ln.strip() not in existing]
    if missing:
        docs.replace_full_text(f["id"], text.rstrip("\n") + "\n" + "\n".join(missing) + "\n")


def repair_metrics(ctx: VerifyContext) -> None:
    metrics = (ctx.step3_result or {}).get("metrics")
    if metrics is None:
        raise RuntimeError("немає рядка метрики для дозапису")
    f = drive.find_file_by_title(METRICS_SHEET_TITLE, RESUME_FOLDER_ID)
    sheet_id = f["id"] if f else drive.create_spreadsheet(METRICS_SHEET_TITLE, RESUME_FOLDER_ID)
    sheets.ensure_header(sheet_id)
    sheets.upsert_row_by_first_cell(sheet_id, metrics.as_row())


def repair_selfcheck(ctx: VerifyContext) -> None:
    result = (ctx.step4_result or {}).get("result")
    if result is None:
        raise RuntimeError("немає результату перевірки для дозапису")
    f = drive.find_file_by_title(SELFCHECK_SHEET_TITLE, RESUME_FOLDER_ID)
    if not f:
        raise RuntimeError("«Результат щоденної перевірки» відсутній у Drive")
    marker = ctx.start_marker or {}
    if marker.get("row") and marker.get("sheet_id") == f["id"]:
        sheets.update_row(f["id"], marker["row"], result.as_row())
    else:
        sheets.append_row(f["id"], result.as_row())


CHECKS = [
    ("лог відсіву", check_reject_log, repair_reject_log),
    ("лог показаних", check_shown_log, repair_shown_log),
    ("метрики", check_metrics, repair_metrics),
    ("результат перевірки", check_selfcheck, repair_selfcheck),
    ("джерела", check_sources, None),
]


def _run_check(fn, ctx) -> tuple[bool, str]:
    try:
        return fn(ctx)
    except Exception as exc:  # noqa: BLE001 - збій самої перевірки = не підтверджено
        logger.exception("verify_writes: перевірка не виконалась")
        return False, f"не вдалось перевірити ({type(exc).__name__}: {exc})"


def verify_writes(ctx: VerifyContext) -> VerifyResult:
    """Перевірка → (для невдалих) одна спроба дозапису → повторна перевірка → тривога НЕ ЗАПИСАНО."""
    failed: list[str] = []
    repaired: list[str] = []
    for name, check, repair in CHECKS:
        ok, why = _run_check(check, ctx)
        if not ok and repair is not None:
            logger.warning("verify_writes: «%s» не пройшло (%s) — одна спроба дозапису", name, why)
            try:
                repair(ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning("verify_writes: дозапис «%s» не вдався: %s", name, exc)
            ok, why = _run_check(check, ctx)
            if ok:
                repaired.append(name)
        if not ok:
            failed.append(why)
    for why in reversed(failed):  # перша причина — першим рядком
        alarm.add_not_written(why)
    return VerifyResult(ok=not failed, failed=failed, repaired=repaired)

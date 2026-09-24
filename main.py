#!/usr/bin/env python3
"""Точка входу Job Search Automation Pipeline.

Запуск: `python main.py` (локально) або через cron/systemd timer/GitHub
Actions schedule (див. README.md, розділ "Планування запуску").

Кожен запуск — незалежна сесія: увесь стан, потрібний для дедублікації
(Крок 1) і метрик (Крок 3), живе в Google Drive/Docs/Sheets, не в пам'яті
процесу — так само, як було задумано в текстовій версії, просто тепер це
реалізовано через справжній API, а не обхідні прийоми.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import CANDIDATE_LOCAL_TZ
from google_services import notify
from pipeline.step1_2_freelance import run_step1_2
from pipeline.step1_vacancies import run_step1
from pipeline.step2_mail import run_step2
from pipeline.step3_metrics import run_step3
from pipeline.step4_selfcheck import format_selfcheck_block, run_step4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def _format_freelance_project(v) -> list[str]:
    lines = [f"- **{v.title}** | {v.category} | [{v.source}]({v.url})"]
    if v.competition_level:
        lines.append(f"  - Кількість ставок: {v.bids_count} ({v.competition_level} конкуренція)")
    lines.append(f"  - Бюджет: {v.budget_raw or 'не вказано'}")
    lines.append(f"  - Дата публікації: {v.posted_date or 'невизначена'}")
    lines.append(f"  - Чому пройшов фільтр: {v.why_relevant}\n")
    return lines


def format_report(
    step1_result: dict,
    step1_2_result: dict,
    step2_result: dict,
    step3_result: dict,
    step4_result: dict,
) -> str:
    lines: list[str] = []
    vacancies = step1_result["vacancies"]

    lines.append("## Крок 1 — нові вакансії\n")
    if not vacancies:
        lines.append("Нових відповідних вакансій не знайдено.\n")
    else:
        for v in vacancies:
            lines.append(f"- **{v.title}** | {v.company} | [{v.source}]({v.url or 'н/д'})")
            lines.append(f"  - Match-рівень: {v.match_level}")
            lines.append(f"  - Оцінка відповідності: {v.match_reasoning}")
            lines.append(f"  - Ветеранські бонуси: {'так' if v.veteran_bonus else 'ні'}")
            loc = "так" if v.low_match_location else "ні"
            if v.low_match_location:
                loc += f" ({v.low_match_location_reason})"
            lines.append(f"  - Low match через локацію: {loc}")
            date_str = "невизначена" if v.date_undetermined else (v.posted_date or "невизначена")
            lines.append(f"  - Дата публікації: {date_str}\n")

    unavailable = [
        s.source for s in step1_result["source_statuses"].values() if s.status == "недоступне"
    ]
    if unavailable:
        lines.append(f"⚠️ Недоступні джерела в цьому запуску: {', '.join(unavailable)}\n")
    if step1_result.get("dedup_log_note"):
        lines.append(f"⚠️ {step1_result['dedup_log_note']}\n")

    lines.append("\n## Крок 1.2 — фріланс-проєкти\n")
    projects = step1_2_result.get("projects", [])
    if not projects:
        lines.append("Нових релевантних фріланс-проєктів не знайдено.\n")
    else:
        for v in projects:
            lines.extend(_format_freelance_project(v))

    fl_unavailable = [
        s.source for s in step1_2_result.get("source_statuses", {}).values() if s.status == "недоступне"
    ]
    if fl_unavailable:
        lines.append(f"⚠️ Недоступні джерела Кроку 1.2 у цьому запуску: {', '.join(fl_unavailable)}\n")
    if step1_2_result.get("dedup_log_note"):
        lines.append(f"⚠️ {step1_2_result['dedup_log_note']}\n")

    lines.append("\n## Крок 2 — пошта\n")
    findings = step2_result.get("findings", [])
    if not findings:
        lines.append("Нових листів не знайдено.\n")
    else:
        for f in findings:
            status_str = f" | Статус: {f.status}" if f.status else ""
            lines.append(f"- {f.sender} | {f.subject} | {f.date}{status_str}")
            lines.append(f"  {f.summary}\n")

    lines.append("\n## Крок 3 — метрики\n")
    if step3_result.get("saved"):
        lines.append("Метрику збережено у «Метрики автопошуку вакансій».")
    else:
        lines.append(f"⚠️ Метрику не вдалося зберегти: {step3_result.get('error')}")

    lines.append("\n## Крок 4 — самоперевірка запуску\n")
    lines.append(format_selfcheck_block(step4_result["result"]))
    if not step4_result.get("saved"):
        lines.append(
            f"\n⚠️ Результат перевірки виведено вище, але у файл «Результат щоденної "
            f"перевірки» продубльовано не було: {step4_result.get('error')}"
        )

    return "\n".join(lines)


def main() -> int:
    # Два навмисно різні "сьогодні" — див. config.py, розділ "Часові пояси",
    # і докладний коментар на початку pipeline/step1_vacancies.py.
    utc_now = datetime.now(timezone.utc)
    utc_today = utc_now.date()
    local_today = datetime.now(ZoneInfo(CANDIDATE_LOCAL_TZ)).date()
    # Один момент, зафіксований ОДИН раз і перевикористаний для Кроку 4
    # (4.4 і 4.5) — аналог одноразового `date -u` в чат-версії промпту.
    selfcheck_timestamp = utc_now.strftime("%Y-%m-%d %H:%M") + " UTC"

    logger.info("=== Запуск пайплайна: %s (UTC) / %s (%s) ===", utc_today.isoformat(), local_today.isoformat(), CANDIDATE_LOCAL_TZ)

    try:
        step1_result = run_step1(utc_today=utc_today, local_today=local_today)
    except Exception:
        logger.exception("Крок 1 критично провалився")
        step1_result = {
            "vacancies": [],
            "total_found_before_filters": 0,
            "source_statuses": {},
            "collection_methods": {},
            "dedup_log_updated": False,
            "dedup_log_note": "Крок 1 критично провалився, див. лог помилок.",
            "dedup_level_used": 0,
        }

    # Крок 1.2 виконується ЗАВЖДИ одразу після Кроку 1, незалежно від його
    # результату — той самий принцип "наступний крок не залежить від успіху
    # попереднього", що й для Кроку 2 нижче.
    try:
        step1_2_result = run_step1_2(utc_today=utc_today, local_today=local_today)
    except Exception:
        logger.exception("Крок 1.2 критично провалився")
        step1_2_result = {
            "projects": [],
            "source_statuses": {},
            "dedup_log_updated": False,
            "dedup_log_note": "Крок 1.2 критично провалився, див. лог помилок.",
            "metrics_saved": False,
            "metrics_error": "Крок 1.2 критично провалився",
            "reviewed_by_category": {},
            "rejected_irrelevant": 0,
        }

    # Крок 2 виконується завжди, незалежно від результату Кроків 1/1.2 (те
    # саме правило, що й у чат-версії промпту) — тому окремий try/except, а
    # не залежність від успіху блоків вище.
    try:
        step2_result = run_step2()
    except Exception:
        logger.exception("Крок 2 критично провалився")
        step2_result = {"findings": [], "total_emails_found": 0, "hr_domain_emails": 0, "known_company_emails": 0}

    # Крок 3/4 + звіт у try/finally: push-повідомлення (нижче) має піти
    # БЕЗУМОВНО, навіть якщо щось у Кроці 3/4/формуванні звіту несподівано
    # впаде — той самий принцип "сповіщення завжди", що й у чат-версії.
    try:
        step3_result = run_step3(step1_result, step2_result, utc_today=utc_today)
        step4_result = run_step4(
            step1_result,
            step2_result,
            step3_result,
            timestamp_utc=selfcheck_timestamp,
            step1_2_result=step1_2_result,
        )

        report = format_report(step1_result, step1_2_result, step2_result, step3_result, step4_result)
        print(report)

        with open(f"reports/report_{utc_today.isoformat()}.md", "w", encoding="utf-8") as f:
            f.write(report)
    except Exception:
        logger.exception("Крок 3/4 або формування звіту критично провалились")
    finally:
        # Наприкінці — push-повідомлення, БЕЗУМОВНО (навіть якщо щось вище
        # провалилось): Володимир хоче знати, коли звіт готовий для
        # перегляду, незалежно від того, чи є в ньому щось "цікаве".
        notify.send_push()

    return 0


if __name__ == "__main__":
    import os

    os.makedirs("reports", exist_ok=True)
    sys.exit(main())

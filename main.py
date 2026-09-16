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
from datetime import date

from pipeline.step1_vacancies import run_step1
from pipeline.step2_mail import run_step2
from pipeline.step3_metrics import run_step3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def format_report(step1_result: dict, step2_result: dict, step3_result: dict) -> str:
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

    return "\n".join(lines)


def main() -> int:
    today = date.today()
    logger.info("=== Запуск пайплайна: %s ===", today.isoformat())

    try:
        step1_result = run_step1(today=today)
    except Exception:
        logger.exception("Крок 1 критично провалився")
        step1_result = {
            "vacancies": [],
            "total_found_before_filters": 0,
            "source_statuses": {},
            "dedup_log_updated": False,
            "dedup_log_note": "Крок 1 критично провалився, див. лог помилок.",
        }

    try:
        step2_result = run_step2()
    except Exception:
        logger.exception("Крок 2 критично провалився")
        step2_result = {"findings": [], "total_emails_found": 0, "hr_domain_emails": 0, "known_company_emails": 0}

    step3_result = run_step3(step1_result, step2_result, today=today)

    report = format_report(step1_result, step2_result, step3_result)
    print(report)

    with open(f"reports/report_{today.isoformat()}.md", "w", encoding="utf-8") as f:
        f.write(report)

    return 0


if __name__ == "__main__":
    import os

    os.makedirs("reports", exist_ok=True)
    sys.exit(main())

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
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from claude_orchestrator.cost import tracker as cost_tracker
from config import CANDIDATE_LOCAL_TZ, CATEGORY_BLOCK_TITLE, CATEGORY_NONE, COMPARISON_SHEET_TITLE, EXIT_CODE_ALARM
from google_services import notify
from pipeline import alarm, schedule_gate
from pipeline.step1_2_freelance import run_step1_2
from pipeline.step1_vacancies import run_step1
from pipeline.cost_log import append_cost_row, build_row
from pipeline.reject_log import write_reject_log
from pipeline.step2_mail import run_step2
from pipeline.step3_metrics import run_step3
from pipeline.step3_tracker import run_step3_tracker
from pipeline.step4_selfcheck import format_selfcheck_block, mark_run_started, run_step4
from pipeline.step5_comparison import format_comparison_block, run_step5

# Windows: при перенаправленні stdout у файл (Task Scheduler -> logs/scheduler.log)
# Python бере системне кодування (cp1250/cp1251), кирилиця ламається, а print()
# звіту міг би впасти з UnicodeEncodeError. Примусово UTF-8 для обох потоків.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os.makedirs("reports", exist_ok=True)
os.makedirs("logs", exist_ok=True)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_root_logger.addHandler(_console_handler)

# RotatingFileHandler, а не basicConfig(filename=...): при щоденному cron
# (README, "Планування запуску") лог інакше ріс би необмежено місяцями.
# 5 файлів по 2 МБ ~= 10 МБ на диску максимум, старе відсікається само.
_file_handler = logging.handlers.RotatingFileHandler(
    "logs/pipeline.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_root_logger.addHandler(_file_handler)

logger = logging.getLogger("main")


_FREELANCE_SOURCE_LABEL = {"freelancehunt.com": "Freelancehunt", "telegram": "Telegram"}


def _format_freelance_project(v) -> list[str]:
    label = _FREELANCE_SOURCE_LABEL.get(v.source, v.source)
    lines = [f"- **{v.title}** | {v.category} | [{label}]({v.url})"]
    if v.competition_level:
        lines.append(f"  - Кількість ставок: {v.bids_count} ({v.competition_level} конкуренція)")
    lines.append(f"  - Бюджет: {v.budget_raw or 'не вказано'}")
    lines.append(f"  - Дата публікації: {v.posted_date or 'невизначена'}")
    lines.append(f"  - Чому пройшов фільтр: {v.why_relevant}\n")
    return lines


def _format_vacancy(v, with_category: bool = False) -> list[str]:
    lines = [f"- **{v.title}** | {v.company} | [{v.source}]({v.url or 'н/д'})"]
    lines.append(f"  - Match-рівень: {v.match_level}")
    # Позначки — у «Оцінка відповідності», як в онлайн-промпті (Крок 1 п.2, Правило И).
    marks = list(v.flags) + (["лише картка"] if v.card_only else [])
    reasoning = v.match_reasoning + (f" [{'; '.join(marks)}]" if marks else "")
    lines.append(f"  - Оцінка відповідності: {reasoning}")
    lines.append(f"  - Ветерани: {'так' if v.veteran_bonus else 'ні'}")
    lines.append(f"  - Інвалідність: {'так' if v.disability else 'ні'}")
    if with_category:
        lines.append(f"  - Категорія: {v.category}")
    loc = "так" if v.low_match_location else "ні"
    if v.low_match_location:
        loc += f" ({v.low_match_location_reason})"
    lines.append(f"  - Обмеження локації: {loc}")
    if v.company_flag:
        lines.append(f"  - Прапорець: {v.company_flag}")
    date_str = "невизначена" if v.date_undetermined else (v.posted_date or "невизначена")
    lines.append(f"  - Дата публікації: {date_str}\n")
    return lines


def format_report(
    step1_result: dict,
    step1_2_result: dict,
    step2_result: dict,
    step3_result: dict,
    step4_result: dict,
    step5_result: dict | None = None,
    alarms: list[str] | None = None,
) -> str:
    lines: list[str] = []
    if alarms:  # ПЕРШІ рядки звіту (консоль, reports/); те саме — на початку push
        lines.append(alarm.format_block(alarms))
        lines.append("")
    vacancies = step1_result["vacancies"]

    lines.append("## Крок 1 — нові вакансії\n")
    # Категорія ≠ «—» (ветеран / інвалідність) — окремий блок після основного
    # списку; фільтри для таких вакансій ті самі (змінюється лише вивід).
    main_list = [v for v in vacancies if v.category == CATEGORY_NONE]
    category_list = [v for v in vacancies if v.category != CATEGORY_NONE]
    if not main_list:
        lines.append("Нових відповідних вакансій не знайдено.\n")
    else:
        for v in main_list:
            lines.extend(_format_vacancy(v))

    lines.append(f"**{CATEGORY_BLOCK_TITLE}**" if category_list else f"{CATEGORY_BLOCK_TITLE}: немає")
    for v in category_list:  # вже відсортовано High → Medium → Low
        lines.extend(_format_vacancy(v, with_category=True))
    lines.append("")

    # Блок «Майже підійшли» — щодня, одразу після основного списку (Правило И п.7).
    near = step1_result.get("near_misses") or []
    lines.append("**Майже підійшли**" + ("" if near else ": немає"))
    for nm in near:
        lines.append(f"- **{nm.title}** | {nm.company} | [{nm.source}]({nm.url}) | не виконано: {nm.unmet}")
    lines.append("")

    # Статуси всіх джерел — окремим рядком після списку (Правило В, И п.8).
    status_parts = []
    for st in step1_result["source_statuses"].values():
        status_parts.append(f"{st.source}: {st.status}")
    if status_parts:
        lines.append("Статуси джерел: " + "; ".join(status_parts) + "\n")

    # Службові подробиці — окремим блоком «Примітки», не всередині списку.
    notes: list[str] = []
    if step1_result.get("dedup_log_note"):
        notes.append(step1_result["dedup_log_note"])
    if step1_result.get("unknown_date_note"):
        notes.append(step1_result["unknown_date_note"])
    if notes:
        lines.append("**Примітки**")
        lines.extend(f"- ⚠️ {n}" for n in notes)
        lines.append("")

    lines.append("\n## Крок 1.2 — фріланс-проєкти\n")
    projects = step1_2_result.get("projects", [])
    if not projects:
        lines.append("Нових релевантних фріланс-проєктів не знайдено.\n")
    else:
        for v in projects:
            lines.extend(_format_freelance_project(v))
        # «не знайдено» окремо по кожному з трьох джерел, якщо різниця є.
        groups = {"BI и аналитика данных": "Freelancehunt — BI и аналитика данных",
                  "Базы данных и SQL": "Freelancehunt — Базы данных и SQL", "Telegram": "Telegram-канал"}
        present = {("Telegram" if v.source == "telegram" else v.category) for v in projects}
        for key, name in groups.items():
            if key not in present:
                lines.append(f"{name}: нових релевантних фріланс-проєктів не знайдено.")
        lines.append("")

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
    for note in step2_result.get("notes") or []:
        lines.append(f"⚠️ {note}")

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

    if step5_result and step5_result.get("active"):
        lines.append("\n\n## Крок 5 — порівняння з автоматичним (чат) запуском\n")
        lines.append(format_comparison_block(step5_result["row"]))
        if not step5_result.get("saved"):
            lines.append(
                f"\n⚠️ Порівняння виведено вище, але у «{COMPARISON_SHEET_TITLE}» "
                f"продубльовано не було: {step5_result.get('error')}"
            )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Код виходу: 0 — штатно; EXIT_CODE_ALARM (2) — була тривога; 1 — аварія поза кроками."""
    argv = sys.argv[1:] if argv is None else argv
    scheduled = "--scheduled" in argv
    if scheduled:
        due, why = schedule_gate.is_due()
        if not due:
            logger.info("Запланований запуск пропущено: %s", why)
            return 0
        schedule_gate.mark_started()
        logger.info("Запланований запуск: %s", why)
    alarm.reset()
    try:
        return _run()
    except Exception as exc:  # noqa: BLE001 - аварія поза окремими кроками
        logger.exception("Аварія пайплайна")
        alarm.add(f"аварія пайплайна: {type(exc).__name__}: {exc}")
        print(alarm.format_block())
        notify.send_push(notify.build_message(alarm.format_block()), urgent=True)
        return 1


def _run() -> int:
    # Два навмисно різні "сьогодні" — див. config.py, розділ "Часові пояси",
    # і докладний коментар на початку pipeline/step1_vacancies.py.
    utc_now = datetime.now(timezone.utc)
    utc_today = utc_now.date()
    local_today = datetime.now(ZoneInfo(CANDIDATE_LOCAL_TZ)).date()
    # Один момент, зафіксований ОДИН раз і перевикористаний для Кроку 4
    # (4.4 і 4.5) — аналог одноразового `date -u` в чат-версії промпту.
    selfcheck_timestamp = utc_now.strftime("%Y-%m-%d %H:%M") + " UTC"

    cost_tracker.reset()  # вартість Claude рахується окремо за кожен запуск
    logger.info("=== Запуск пайплайна: %s (UTC) / %s (%s) ===", utc_today.isoformat(), local_today.isoformat(), CANDIDATE_LOCAL_TZ)

    # Мітка початку: рядок «ЗАПУСК ПОЧАТО» у «Результат щоденної перевірки»; Крок 4 оновить його
    # наприкінці. Якщо запуск обірветься — мітка лишиться як свідчення.
    start_marker = mark_run_started(selfcheck_timestamp)

    try:
        step1_result = run_step1(utc_today=utc_today, local_today=local_today)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Крок 1 критично провалився")
        alarm.add(f"крок 1 критично провалився: {type(exc).__name__}: {exc}")
        step1_result = {
            "vacancies": [],
            "total_found_before_filters": 0,
            "source_statuses": {},
            "collection_methods": {},
            "dedup_log_updated": False,
            "dedup_log_note": "Крок 1 критично провалився, див. лог помилок.",
            "dedup_level_used": 0,
        }

    # Діагностичний лог причин відсіву (порівняння з онлайн-версією): локальний файл ->
    # Drive (+1 повтор) -> перевірка кількості рядків. Збій не валить решту запуску, але = тривога.
    try:
        rl = write_reject_log(step1_result.get("decisions", []), utc_today)
        if not rl["verified"]:
            alarm.add(f"лог відсіву не вивантажено в Drive або кількість рядків не збіглась ({rl['error']}); "
                      f"локальна копія: {rl['local_path']}")
            note = step1_result.get("dedup_log_note") or ""
            step1_result["dedup_log_note"] = (note + "; " if note else "") + f"лог причин відсіву не підтверджено в Drive: {rl['error']}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Лог причин відсіву не записано")
        alarm.add(f"лог відсіву не записано: {type(exc).__name__}: {exc}")
        note = step1_result.get("dedup_log_note") or ""
        step1_result["dedup_log_note"] = (note + "; " if note else "") + f"лог причин відсіву не записано: {exc}"

    # Крок 1.2 виконується ЗАВЖДИ одразу після Кроку 1, незалежно від його
    # результату — той самий принцип "наступний крок не залежить від успіху
    # попереднього", що й для Кроку 2 нижче.
    try:
        step1_2_result = run_step1_2(utc_today=utc_today, local_today=local_today)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Крок 1.2 критично провалився")
        alarm.add(f"крок 1.2 критично провалився: {type(exc).__name__}: {exc}")
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("Крок 2 критично провалився")
        alarm.add(f"крок 2 критично провалився: {type(exc).__name__}: {exc}")
        step2_result = {"findings": [], "total_emails_found": 0, "hr_domain_emails": 0, "known_company_emails": 0,
                        "error": "Крок 2 критично провалився, див. лог помилок"}

    # Крок 3/4 + звіт у try/finally: push-повідомлення (нижче) має піти
    # БЕЗУМОВНО, навіть якщо щось у Кроці 3/4/формуванні звіту несподівано
    # впаде — той самий принцип "сповіщення завжди", що й у чат-версії.
    report_printed = False
    try:
        step3_result = run_step3(step1_result, step2_result, utc_today=utc_today)
        # Крок 3 (таблиця відгуків) — власний try: збій запису в ручну
        # таблицю не повинен ховати метрики/самоперевірку/звіт.
        try:
            step3_tracker_result = run_step3_tracker(step2_result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Крок 3 (таблиця відгуків) провалився")
            step3_tracker_result = {"status": "НЕ ВИКОНАНО", "note": f"помилка: {exc}", "applied": [], "skipped": []}
        step4_result = run_step4(
            step1_result,
            step2_result,
            step3_result,
            timestamp_utc=selfcheck_timestamp,
            step1_2_result=step1_2_result,
            step3_tracker_result=step3_tracker_result,
            start_marker=start_marker,
        )
        # Тривоги з результатів кроків (джерела, Claude, запис у Drive) — ДО формування звіту,
        # щоб потрапити в його перші рядки.
        alarm.collect_from_results(step1_result, step1_2_result, step2_result, step3_result, step4_result)

        # Крок 5 — порівняння з автоматичним (чат) запуском того самого дня.
        # Сам себе відключає (result["active"] == False), якщо
        # config.SERVICE_FILE_TITLE_SUFFIX порожній — тестового режиму
        # немає, порівнювати канонічний журнал з самим собою нема сенсу.
        # Окремий try/except: збій порівняння не повинен ховати вже готовий
        # звіт по Кроках 1-4.
        try:
            step5_result = run_step5(step1_result, step1_2_result, step4_result, utc_today=utc_today)
        except Exception:
            logger.exception("Крок 5 (порівняння) критично провалився")
            step5_result = {"active": False}

        # Журнал вартості logs/costs.csv (docs/COSTS.md) — окремий try: збій
        # запису не повинен ховати звіт.
        try:
            append_cost_row(build_row(selfcheck_timestamp, step1_result, step4_result["result"], step5_result))
        except Exception:
            logger.exception("Запис у logs/costs.csv не вдався")

        report = format_report(
            step1_result, step1_2_result, step2_result, step3_result, step4_result, step5_result,
            alarms=alarm.reasons(),
        )
        print(report)
        report_printed = True

        with open(f"reports/report_{utc_today.isoformat()}.md", "w", encoding="utf-8") as f:
            f.write(report)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Крок 3/4 або формування звіту критично провалились")
        alarm.add(f"крок 3/4 або формування звіту критично провалились: {type(exc).__name__}: {exc}")
    finally:
        # Наприкінці — push-повідомлення, БЕЗУМОВНО (навіть якщо щось вище
        # провалилось): Володимир хоче знати, коли звіт готовий для
        # перегляду, незалежно від того, чи є в ньому щось "цікаве".
        # Тривога — на початку повідомлення (і в консолі, якщо звіт не надруковано).
        logger.info("Вартість Claude за запуск: $%.4f (%d викликів)", cost_tracker.total_usd, cost_tracker.calls)
        if alarm.active() and not report_printed:
            print(alarm.format_block())
        sent = notify.send_push(notify.build_message(alarm.format_block()), urgent=alarm.active())
        if alarm.active() and not sent:
            logger.error("Push із тривогою не надіслано — див. консоль/звіт: %s", "; ".join(alarm.reasons()))

    return EXIT_CODE_ALARM if alarm.active() else 0


if __name__ == "__main__":
    sys.exit(main())

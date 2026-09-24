"""Регресійний тест на фікс "{today} у промпті": дата більше не живе як
плейсхолдер всередині констант VACANCY_EVAL_SYSTEM_PROMPT /
FREELANCE_EVAL_SYSTEM_PROMPT (раніше — {{today}} у f-string + .replace(),
крихка подвійна механіка поруч із {{ }}-екрануванням JSON-схеми в тому ж
рядку) — тепер підставляється рівно один раз, простим конкатом, як і в
build_email_classify_prompt."""
from __future__ import annotations

from datetime import date

from claude_orchestrator.prompts import (
    FREELANCE_EVAL_SYSTEM_PROMPT,
    VACANCY_EVAL_SYSTEM_PROMPT,
    build_freelance_eval_prompt,
    build_vacancy_eval_prompt,
)
from models import RawFreelanceProject, RawJobPosting


def test_no_leftover_today_placeholder_in_constants():
    assert "{today}" not in VACANCY_EVAL_SYSTEM_PROMPT
    assert "{today}" not in FREELANCE_EVAL_SYSTEM_PROMPT


def test_vacancy_prompt_mentions_today_exactly_once():
    job = RawJobPosting(
        source="djinni.co",
        title="Data Analyst",
        company="Acme",
        url="https://example.com/1",
        posted_raw="3 days ago",
        salary_raw="",
        description_snippet="",
    )
    today = date(2026, 9, 24)
    prompt = build_vacancy_eval_prompt([job], today=today)
    assert prompt.count(today.isoformat()) == 1
    assert "Сьогоднішня дата: 2026-09-24" in prompt


def test_freelance_prompt_mentions_today_exactly_once():
    project = RawFreelanceProject(
        source="freelancehunt.com",
        title="SQL-звіт по продажах",
        url="https://freelancehunt.com/project/1.html",
        category="Базы данных и SQL",
        posted_raw="сьогодні",
        bids_count=3,
    )
    today = date(2026, 9, 24)
    prompt = build_freelance_eval_prompt([project], today=today)
    assert prompt.count(today.isoformat()) == 1
    assert "Сьогоднішня дата: 2026-09-24" in prompt

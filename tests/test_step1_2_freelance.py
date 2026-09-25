"""Тести для pipeline/step1_2_freelance.py — рівні конкуренції за
кількістю ставок, бінарна релевантність (на відміну від Кроку 1, без
градації Match-рівня), дедублікація за URL, підрахунок відхилених.
Google Drive/Docs/Sheets та Claude API замоковано."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pipeline.step1_2_freelance as step1_2
from models import RawFreelanceProject, SourceStatus


def _project(i: int, *, source="freelancehunt.com", category="BI и аналитика данных", bids=None):
    return RawFreelanceProject(
        source=source,
        title=f"Проєкт {i}",
        url=f"https://freelancehunt.com/project/{i}.html",
        category=category,
        posted_raw="",
        bids_count=bids,
        budget_raw="",
        description_snippet="",
    )


def test_competition_level_thresholds():
    # Пороги каліброві на пілотному запуску 22.09.2026 (config.py):
    # FREELANCEHUNT_LOW_BIDS_MAX=14, FREELANCEHUNT_MEDIUM_BIDS_MAX=40.
    assert step1_2._competition_level(None) is None  # Telegram — без даних про ставки
    assert step1_2._competition_level(14) == "низька"
    assert step1_2._competition_level(15) == "середня"
    assert step1_2._competition_level(40) == "середня"
    assert step1_2._competition_level(41) == "висока"


def _run(projects, evals, *, dedup=(None, "")):
    statuses = {
        "freelancehunt.com": SourceStatus(source="freelancehunt.com", status="OK"),
        "telegram": SourceStatus(source="telegram", status="OK"),
    }
    with patch.object(step1_2, "_scrape_all", lambda: (projects, statuses)), \
         patch.object(step1_2, "call_json", lambda prompt: {"evaluations": evals}), \
         patch.object(step1_2, "_read_dedup_log_with_retry", lambda: dedup), \
         patch.object(step1_2, "_get_or_create_metrics_sheet", lambda: "sheet123"), \
         patch("google_services.sheets.ensure_header", lambda *a, **k: None), \
         patch("google_services.sheets.append_row", lambda *a, **k: None), \
         patch("google_services.sheets.read_last_data_rows", lambda *a, **k: []), \
         patch("google_services.docs.replace_full_text", lambda *a, **k: None):
        return step1_2.run_step1_2(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))


def test_relevant_and_rejected_counting():
    projects = [_project(0, bids=5), _project(1, bids=5)]
    evals = [
        {"raw_index": 0, "is_relevant": True, "why_relevant": "SQL-звіт", "posted_date": "2026-09-24", "date_undetermined": False},
        {"raw_index": 1, "is_relevant": False, "why_relevant": None, "posted_date": None, "date_undetermined": True},
    ]
    result = _run(projects, evals)

    assert len(result["projects"]) == 1
    assert result["rejected_irrelevant"] == 1
    assert result["projects"][0].competition_level == "низька"
    assert result["metrics_saved"] is True


def test_dedup_by_url_skips_seen_project():
    projects = [_project(0, bids=5)]
    evals = [{"raw_index": 0, "is_relevant": True, "why_relevant": "SQL", "posted_date": None, "date_undetermined": True}]
    log_text = f"2026-09-20 | {projects[0].url} | Проєкт 0 — BI и аналитика данных\n"

    result = _run(projects, evals, dedup=("doc1", log_text))

    assert result["projects"] == []


def test_date_outside_window_is_filtered_out():
    projects = [_project(0, bids=5)]
    evals = [{"raw_index": 0, "is_relevant": True, "why_relevant": "SQL", "posted_date": "2020-01-01", "date_undetermined": False}]

    result = _run(projects, evals)

    assert result["projects"] == []


def test_no_relevant_projects_still_saves_metrics():
    """Крок 1.2 має зберегти метрику (0/0/0), навіть якщо всі джерела
    порожні/нерелевантні — так само, як Крок 1 не пропускає Крок 3 при
    total_found=0."""
    result = _run([], [])

    assert result["projects"] == []
    assert result["metrics_saved"] is True


def test_known_url_skips_claude_call_entirely():
    """Регресійний тест на оптимізацію: усі URL уже в лозі дублів — Claude
    не повинен викликатись узагалі (той самий принцип, що й у Кроці 1)."""
    projects = [_project(0, bids=5), _project(1, bids=5)]
    log_text = (
        f"2026-09-20 | {projects[0].url} | Проєкт 0 — BI и аналитика данных\n"
        f"2026-09-20 | {projects[1].url} | Проєкт 1 — BI и аналитика данных\n"
    )
    call_count = 0

    def _counting_call_json(prompt):
        nonlocal call_count
        call_count += 1
        return {"evaluations": []}

    statuses = {
        "freelancehunt.com": SourceStatus(source="freelancehunt.com", status="OK"),
        "telegram": SourceStatus(source="telegram", status="OK"),
    }
    with patch.object(step1_2, "_scrape_all", lambda: (projects, statuses)), \
         patch.object(step1_2, "call_json", _counting_call_json), \
         patch.object(step1_2, "_read_dedup_log_with_retry", lambda: ("doc1", log_text)), \
         patch.object(step1_2, "_get_or_create_metrics_sheet", lambda: "sheet123"), \
         patch("google_services.sheets.ensure_header", lambda *a, **k: None), \
         patch("google_services.sheets.append_row", lambda *a, **k: None), \
         patch("google_services.sheets.read_last_data_rows", lambda *a, **k: []), \
         patch("google_services.docs.replace_full_text", lambda *a, **k: None):
        result = step1_2.run_step1_2(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert call_count == 0, "усі URL вже відомі — Claude не мав викликатись жодного разу"
    assert result["projects"] == []
    assert result["rejected_irrelevant"] == 0, "відомі дублікати не рахуються як «відхилені нерелевантні»"


def test_parse_dedup_log_handles_pipe_in_label():
    """Регресійний тест на maxsplit=2: назва проєкту з '|' не має обрізати
    label на першому зайвому символі."""
    text = "2026-09-20 | https://freelancehunt.com/project/9.html | Парсинг | вивантаження — BI и аналитика данных\n"
    entries = step1_2._parse_dedup_log(text)
    assert len(entries) == 1
    assert entries[0].label == "Парсинг | вивантаження — BI и аналитика данных"

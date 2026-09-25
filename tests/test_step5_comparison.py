"""Тести для pipeline/step5_comparison.py — порівняння автоматичного (чат)
і ручного (Python) запуску того самого дня. Google Drive/Docs/Sheets
замоковано; читання канонічних файлів імітується словником {назва: дані}."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pipeline.step5_comparison as step5
from models import METRICS_HEADER, SelfCheckResult, ScoredFreelanceProject, ScoredVacancy


def _vacancy(url: str) -> ScoredVacancy:
    return ScoredVacancy(
        title="Data Analyst",
        company="Co",
        source="djinni.co",
        url=url,
        match_level="Medium",
        match_reasoning="ok",
        veteran_bonus=False,
        low_match_location=False,
        low_match_location_reason="",
        posted_date="2026-09-25",
        date_undetermined=False,
    )


def _freelance_project(url: str) -> ScoredFreelanceProject:
    return ScoredFreelanceProject(
        title="Проєкт",
        source="freelancehunt.com",
        category="BI и аналитика данных",
        url=url,
        why_relevant="SQL",
        posted_date="2026-09-25",
    )


def test_inactive_when_no_suffix():
    with patch.object(step5, "SERVICE_FILE_TITLE_SUFFIX", ""):
        result = step5.run_step5({}, {}, {}, utc_today=date(2026, 9, 25))
    assert result == {"active": False}


def test_computes_overlap_and_diff_correctly():
    today = date(2026, 9, 25)
    today_str = today.isoformat()

    # --- Дані автоматичного (чат) запуску ---
    v_metrics_row = ["2026-09-25"] + ["0"] * (len(METRICS_HEADER) - 1)
    v_metrics_row[METRICS_HEADER.index(
        "Загальна кількість вакансій, знайдених на всіх джерелах до фільтрації"
    )] = "80"
    v_metrics_row[METRICS_HEADER.index("Показані вакансії (пройшли всі фільтри)")] = "2"

    vacancy_dedup_text = (
        "2026-09-25 | https://example.com/shared | Data Analyst — SharedCo\n"
        "2026-09-25 | https://example.com/auto-only | Data Analyst — AutoOnlyCo\n"
        "2026-09-24 | https://example.com/old | Data Analyst — OldCo\n"
    )

    selfcheck_row = [
        "2026-09-25 09:17 UTC", "OK", "OK", "OK", "OK", "5/5",
        "перша_сторінка", "3", "так", "ні", "без зауважень", "OK",
    ]

    files_by_title = {
        step5.METRICS_SHEET_TITLE_CANONICAL: {"id": "v-metrics-id"},
        step5.FREELANCE_METRICS_SHEET_TITLE_CANONICAL: None,  # недоступний цього разу
        step5.SELFCHECK_SHEET_TITLE_CANONICAL: {"id": "selfcheck-id"},
        step5.DEDUP_LOG_TITLE_CANONICAL: {"id": "v-dedup-id"},
        step5.FREELANCE_DEDUP_LOG_TITLE_CANONICAL: None,
        step5.COMPARISON_SHEET_TITLE: {"id": "comparison-id"},
    }

    def _find_file(title, folder_id):
        return files_by_title.get(title)

    def _read_values(file_id):
        if file_id == "v-metrics-id":
            return [METRICS_HEADER, v_metrics_row]
        if file_id == "selfcheck-id":
            from models import SELFCHECK_HEADER
            return [SELFCHECK_HEADER, selfcheck_row]
        raise AssertionError(f"unexpected read_values call: {file_id}")

    def _read_full_text(file_id):
        if file_id == "v-dedup-id":
            return vacancy_dedup_text
        raise AssertionError(f"unexpected read_full_text call: {file_id}")

    appended_rows = []

    # --- Дані ручного (Python) запуску ---
    step1_result = {
        "vacancies": [_vacancy("https://example.com/shared"), _vacancy("https://example.com/manual-only")],
        "total_found_before_filters": 60,
    }
    step1_2_result = {"projects": []}
    step4_result = {
        "result": SelfCheckResult(
            timestamp_utc="2026-09-25 09:20 UTC",
            step1_status="ЧАСТКОВО",
            step1_note="джерел перевірено 4/5",
            step2_status="OK", step2_note="",
            step3_metrics_status="OK", step3_metrics_note="",
            step3_tracker_status="НЕ ВИКОНАНО", step3_tracker_note="",
            sources_ok_count=4,
            collection_method="перша_сторінка",
            conversion_pct="3",
            trend_comparable=False, trend_comparable_reason="4/5",
            only_low_or_zero_at_full_coverage="н/д",
            low_match_streak_signal=False,
        )
    }

    with patch.object(step5, "SERVICE_FILE_TITLE_SUFFIX", " (тест гітхаб)"), \
         patch("google_services.drive.find_file_by_title", side_effect=_find_file), \
         patch("google_services.sheets.read_values", side_effect=_read_values), \
         patch("google_services.docs.read_full_text", side_effect=_read_full_text), \
         patch("google_services.sheets.ensure_header", lambda *a, **k: None), \
         patch("google_services.sheets.append_row", lambda sid, row: appended_rows.append(row)):
        result = step5.run_step5(step1_result, step1_2_result, step4_result, utc_today=today)

    assert result["active"] is True
    assert result["saved"] is True
    row = result["row"]

    assert row.v_found_auto == 80
    assert row.v_found_manual == 60
    assert row.v_shown_auto == 2
    assert row.v_shown_manual == 2
    assert row.v_url_overlap == 1
    assert row.v_url_only_auto == 1
    assert row.v_url_only_manual == 1

    assert row.f_shown_auto is None  # канонічна таблиця фрілансу "недоступна"
    assert row.f_shown_manual == 0
    assert row.f_url_overlap is None

    assert row.step4_sources_auto == "5/5"
    assert row.step4_sources_manual == "4/5"
    assert row.step4_status_auto == "OK"
    assert row.step4_status_manual == "ЧАСТКОВО"

    assert "автоматичні метрики фрілансу" in row.notes
    assert "автоматична самоперевірка" not in row.notes  # selfcheck БУВ доступний

    assert len(appended_rows) == 1
    assert appended_rows[0] == row.as_row()

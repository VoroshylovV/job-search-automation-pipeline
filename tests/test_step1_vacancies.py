"""Тести для pipeline/step1_vacancies.py — вікно дат "сьогодні/вчора",
ліміт UNKNOWN_DATE_FALLBACK_LIMIT для вакансій з невизначеною датою,
дворівнева дедублікація. Google Drive/Docs та виклик Claude API повністю
замоковано (unittest.mock.patch) — тести не потребують ні credentials/,
ні мережі, ні ANTHROPIC_API_KEY."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pipeline.step1_vacancies as step1
from models import RawJobPosting, SourceStatus


def _job(i: int) -> RawJobPosting:
    return RawJobPosting(
        source="djinni.co",
        title=f"Data Analyst {i}",
        company=f"Company {i}",
        url=f"https://example.com/{i}",
        posted_raw="",
        salary_raw="",
        description_snippet="",
    )


def _fake_scrape_all(jobs: list[RawJobPosting]):
    def _inner(today):
        statuses = {"djinni.co": SourceStatus(source="djinni.co", status="OK")}
        methods = {"djinni.co": "перша_сторінка"}
        return jobs, statuses, methods

    return _inner


def _eval(raw_index: int, *, passes=True, posted_date=None, date_undetermined=False) -> dict:
    return {
        "raw_index": raw_index,
        "passes_criteria": passes,
        "reject_reason": None,
        "normalized_title": f"Data Analyst {raw_index}",
        "normalized_company": f"Company {raw_index}",
        "match_level": "Medium",
        "match_reasoning": "ok",
        "veteran_bonus": False,
        "low_match_location": False,
        "low_match_location_reason": None,
        "posted_date": posted_date,
        "date_undetermined": date_undetermined,
    }


def test_date_outside_window_is_filtered_out():
    jobs = [_job(0)]
    evals = [_eval(0, posted_date="2020-01-01")]  # давно, поза вікном "сьогодні/вчора"

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", lambda prompt: {"evaluations": evals}), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        result = step1.run_step1(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert result["vacancies"] == []


def test_date_within_window_passes():
    jobs = [_job(0)]
    evals = [_eval(0, posted_date="2026-09-24")]  # сьогодні

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", lambda prompt: {"evaluations": evals}), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        result = step1.run_step1(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert len(result["vacancies"]) == 1


def test_unknown_date_limit_caps_pass_through():
    """Регресійний тест на фікс: раніше date_undetermined=True вакансії
    проходили без жодного обмеження (UNKNOWN_DATE_FALLBACK_LIMIT був
    оголошений у config.py, але ніде не використовувався)."""
    jobs = [_job(i) for i in range(5)]
    evals = [_eval(i, date_undetermined=True) for i in range(5)]

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", lambda prompt: {"evaluations": evals}), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")), \
         patch.object(step1, "UNKNOWN_DATE_FALLBACK_LIMIT", 2):
        result = step1.run_step1(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert len(result["vacancies"]) == 2
    assert result["unknown_date_note"]


def test_unknown_date_note_empty_when_limit_not_hit():
    jobs = [_job(0)]
    evals = [_eval(0, date_undetermined=True)]

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", lambda prompt: {"evaluations": evals}), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        result = step1.run_step1(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert len(result["vacancies"]) == 1
    assert result["unknown_date_note"] == ""


def test_dedup_level1_filters_seen_urls():
    jobs = [_job(0)]
    evals = [_eval(0, date_undetermined=True)]
    log_text = "2026-09-20 | https://example.com/0 | Data Analyst 0 — Company 0\n"

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", lambda prompt: {"evaluations": evals}), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: ("doc123", log_text)), \
         patch("google_services.docs.replace_full_text", lambda *a, **k: None):
        result = step1.run_step1(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert result["vacancies"] == []
    assert result["dedup_level_used"] == 1


def test_dedup_level2_fallback_when_log_unavailable():
    jobs = [_job(0)]
    evals = [_eval(0, date_undetermined=True)]

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", lambda prompt: {"evaluations": evals}), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, None)), \
         patch.object(step1, "_level2_tracker_urls", lambda: ({"https://example.com/0"}, "рівень 2 активний")):
        result = step1.run_step1(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert result["dedup_level_used"] == 2
    assert result["vacancies"] == []  # url вже є в таблиці відгуків


def test_no_dedup_available_shows_without_filtering():
    jobs = [_job(0)]
    evals = [_eval(0, date_undetermined=True)]

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", lambda prompt: {"evaluations": evals}), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, None)), \
         patch.object(step1, "_level2_tracker_urls", lambda: (None, "обидва рівні недоступні")):
        result = step1.run_step1(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert result["dedup_level_used"] == 0
    assert len(result["vacancies"]) == 1


def test_known_url_skips_claude_call_entirely():
    """Регресійний тест на оптимізацію: вакансія з URL, уже присутнім у
    дедуп-лозі, має бути відфільтрована ДО виклику Claude (не лише після) —
    call_json не повинен викликатись узагалі, якщо всі URL уже відомі."""
    jobs = [_job(0), _job(1)]
    log_text = (
        "2026-09-20 | https://example.com/0 | Data Analyst 0 — Company 0\n"
        "2026-09-20 | https://example.com/1 | Data Analyst 1 — Company 1\n"
    )
    call_count = 0

    def _counting_call_json(prompt):
        nonlocal call_count
        call_count += 1
        return {"evaluations": []}

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", _counting_call_json), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: ("doc123", log_text)), \
         patch("google_services.docs.replace_full_text", lambda *a, **k: None):
        result = step1.run_step1(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert call_count == 0, "усі URL вже відомі — Claude не мав викликатись жодного разу"
    assert result["vacancies"] == []


def test_known_url_partial_prefilter_only_evaluates_unknown():
    """Із двох вакансій лише одна вже відома — Claude має оцінити рівно ту,
    що не в лозі."""
    jobs = [_job(0), _job(1)]
    log_text = "2026-09-20 | https://example.com/0 | Data Analyst 0 — Company 0\n"
    seen_prompts: list[str] = []

    def _capturing_call_json(prompt):
        seen_prompts.append(prompt)
        return {"evaluations": [_eval(0, posted_date="2026-09-24")]}

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", _capturing_call_json), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: ("doc123", log_text)), \
         patch("google_services.docs.replace_full_text", lambda *a, **k: None):
        result = step1.run_step1(utc_today=date(2026, 9, 24), local_today=date(2026, 9, 24))

    assert len(seen_prompts) == 1
    assert "example.com/0" not in seen_prompts[0]
    assert "example.com/1" in seen_prompts[0]
    assert len(result["vacancies"]) == 1
    assert result["vacancies"][0].url == "https://example.com/1"


def test_parse_dedup_log_handles_pipe_in_label():
    """Регресійний тест на maxsplit=2: назва посади з '|' не має обрізати
    label на першому зайвому символі."""
    text = "2026-09-20 | https://example.com/5 | Data Analyst | BI — Company 5\n"
    entries = step1._parse_dedup_log(text)
    assert len(entries) == 1
    assert entries[0].label == "Data Analyst | BI — Company 5"


def test_applied_urls_are_excluded_even_when_log_works():
    """Вакансія з таблиці відгуків (на неї вже подано) не йде в звіт, навіть
    якщо лог показаних доступний і її там немає (регресія 25.09.2026)."""
    jobs = [
        RawJobPosting(source="work.ua", title="Аналітик", company="", url="https://www.work.ua/jobs/7402564/",
                      posted_raw="", salary_raw="", description_snippet=""),
    ]
    calls = []

    def fake_call_json(prompt):
        calls.append(prompt)
        return {"evaluations": []}

    with patch.object(step1, "_scrape_all", _fake_scrape_all(jobs)), \
         patch.object(step1, "call_json", fake_call_json), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: ("doc123", "")), \
         patch.object(step1, "_applied_urls", lambda: {"https://work.ua/jobs/7402564"}), \
         patch("google_services.docs.replace_full_text", lambda *a, **k: None):
        result = step1.run_step1(utc_today=date(2026, 9, 25), local_today=date(2026, 9, 25))
    assert calls == []  # відфільтровано ДО платного виклику Claude
    assert result["vacancies"] == []

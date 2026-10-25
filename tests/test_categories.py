"""Категорії «ветеран» / «інвалідність» (25.10.2026) — лише спосіб показу.
Фільтри не змінюються: вакансія з категорією відсіюється так само, як без неї;
пройшла — іде в окремий блок «Для ветеранів та осіб з інвалідністю»."""
from __future__ import annotations

import importlib
from datetime import date
from unittest.mock import patch

import pytest

import pipeline.step1_vacancies as step1
from claude_orchestrator.prompts import build_vacancy_eval_parts
from config import CATEGORY_BLOCK_TITLE, DISABILITY_KEYWORDS
from models import REJECT_LOG_HEADER, RawJobPosting, SourceStatus
from pipeline.reject_log import build_rows

TODAY = date(2026, 10, 25)
_REAL_STAGE2 = importlib.import_module("pipeline.step1_vacancies").__dict__.get("_confirm_with_full_text")


def _job(i: int, snippet: str = "remote") -> RawJobPosting:
    return RawJobPosting(source="djinni.co", title=f"Data Analyst {i}", company=f"Company {i}",
                         url=f"https://example.com/{i}", posted_raw="", salary_raw="", description_snippet=snippet)


def _ev(i: int, *, level="Medium", veteran=False, passes=True, code=None) -> dict:
    return {
        "raw_index": i, "passes_criteria": passes, "reject_reason": None if passes else "x", "reject_code": code,
        "normalized_title": f"Data Analyst {i}", "normalized_company": f"Company {i}",
        "match_level": level, "match_reasoning": "ok", "veteran_bonus": veteran,
        "low_match_location": False, "low_match_location_reason": None,
        "posted_date": TODAY.isoformat(), "date_undetermined": False,
    }


def _run(jobs, evals):
    statuses = {"djinni.co": SourceStatus(source="djinni.co", status="OK")}
    with patch.object(step1, "_scrape_all", lambda today: (jobs, statuses, {"djinni.co": "перша_сторінка"})), \
         patch.object(step1, "call_json", lambda prompt, **kw: {"evaluations": evals}), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        return step1.run_step1(utc_today=TODAY, local_today=TODAY)


def _report(res) -> str:
    main = importlib.import_module("main")
    step_ok = {"result": {}}
    with patch.object(main, "format_selfcheck_block", lambda r: ""):
        return main.format_report(res, {}, {}, {}, step_ok)


def _category_of(res, i):
    return next(d["category"] for d in res["decisions"] if d["url"] == f"https://example.com/{i}")


# --- визначення категорій --------------------------------------------------
@pytest.mark.parametrize("kw", DISABILITY_KEYWORDS)
def test_each_disability_keyword_detected_ignoring_case(kw):
    assert step1.detect_disability(f"Ми пропонуємо: {kw.upper()}, віддалено")
    assert step1.detect_disability(f"... {kw.lower()} ...")


def test_disability_keywords_exact_list():
    assert DISABILITY_KEYWORDS == (
        "особи з інвалідністю", "люди з інвалідністю", "з інвалідністю", "ЛОВЗ", "інклюзивн",
        "inclusive hiring", "persons with disabilities", "people with disabilities", "disability",
    )


def test_no_keywords_no_disability():
    assert not step1.detect_disability("Data Analyst, SQL, Python, remote")
    assert not step1.detect_disability(None)


# --- п. E: п'ять випадків ----------------------------------------------------
def test_veteran_vacancy_shown_in_category_block():
    res = _run([_job(0)], [_ev(0, veteran=True)])
    v = res["vacancies"][0]
    assert v.category == "ветеран" and v.veteran_bonus and not v.disability
    assert _category_of(res, 0) == "ветеран"
    text = _report(res)
    assert text.index(CATEGORY_BLOCK_TITLE) < text.index("Data Analyst 0") < text.index("Майже підійшли")
    assert "  - Категорія: ветеран" in text
    assert "  - Ветерани: так" in text and "  - Інвалідність: ні" in text


def test_disability_vacancy_shown_in_category_block():
    res = _run([_job(0, "Запрошуємо ЛОВЗ. Remote")], [_ev(0)])
    v = res["vacancies"][0]
    assert v.category == "інвалідність" and v.disability and not v.veteran_bonus
    assert _category_of(res, 0) == "інвалідність"
    text = _report(res)
    assert "  - Категорія: інвалідність" in text and "  - Інвалідність: так" in text


def test_both_categories():
    res = _run([_job(0, "Inclusive hiring, remote")], [_ev(0, veteran=True)])
    assert res["vacancies"][0].category == "обидві"
    assert _category_of(res, 0) == "обидві"
    assert "  - Категорія: обидві" in _report(res)


def test_category_vacancy_failing_salary_filter_rejected_like_uncategorized():
    plain = _run([_job(0, "remote")], [_ev(0, passes=False, code="ЗП")])
    cat = _run([_job(0, "для ЛОВЗ, remote")], [_ev(0, passes=False, code="ЗП", veteran=True)])
    assert plain["vacancies"] == [] and cat["vacancies"] == []
    d_plain, d_cat = plain["decisions"][0], cat["decisions"][0]
    assert (d_cat["result"], d_cat["code"], d_cat["detail"]) == (d_plain["result"], d_plain["code"], d_plain["detail"]) == ("відсіяно", "ЗП", "x")
    assert d_plain["category"] == "—" and d_cat["category"] == "обидві"
    assert _report(cat).count(CATEGORY_BLOCK_TITLE + ": немає") == 1


def test_vacancy_without_category_stays_in_main_list():
    res = _run([_job(0)], [_ev(0)])
    assert res["vacancies"][0].category == "—" and _category_of(res, 0) == "—"
    text = _report(res)
    assert text.index("Data Analyst 0") < text.index(CATEGORY_BLOCK_TITLE + ": немає") < text.index("Майже підійшли")
    assert "Категорія:" not in text.replace(CATEGORY_BLOCK_TITLE, "")


# --- вивід, облік, лог ------------------------------------------------------
def test_block_order_high_medium_low_and_counted_as_shown(monkeypatch):
    # Low потрапляє в показ лише після етапу 2 (на картці — відсів), тому етап 2 справжній.
    monkeypatch.setattr(step1, "_confirm_with_full_text", _REAL_STAGE2)
    jobs = [_job(i, "ЛОВЗ") for i in range(3)] + [_job(3)]
    card = [_ev(0), _ev(1, level="High"), _ev(2), _ev(3)]
    full = [_ev(0, level="Low"), _ev(1, level="High"), _ev(2), _ev(3)]
    statuses = {"djinni.co": SourceStatus(source="djinni.co", status="OK")}

    def fake_call_json(prompt, **kw):
        evals = full if kw.get("stage") == "fulltext" else card
        return {"evaluations": evals}

    with patch.object(step1, "_scrape_all", lambda today: (jobs, statuses, {"djinni.co": "перша_сторінка"})), \
         patch.object(step1, "call_json", fake_call_json), \
         patch.object(step1, "fetch_full_text", lambda job: "Повністю віддалено. Запрошуємо ЛОВЗ." if job.url != "https://example.com/3" else "Повністю віддалено."), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)
    assert len(res["vacancies"]) == 4  # лічильник «показано» і лог показаних включають блок
    text = _report(res)
    block = text[text.index(CATEGORY_BLOCK_TITLE):text.index("Майже підійшли")]
    order = [block.index(f"Data Analyst {i}**") for i in (1, 2, 0)]  # High, Medium, Low
    assert order == sorted(order)
    assert "Data Analyst 3**" not in block and "Data Analyst 3**" in text
    assert len([d for d in res["decisions"] if d["result"] == "показано"]) == 4


def test_disability_keyword_only_on_full_page(monkeypatch):
    monkeypatch.setattr(step1, "_confirm_with_full_text", _REAL_STAGE2)
    jobs = [_job(0, "remote")]
    statuses = {"djinni.co": SourceStatus(source="djinni.co", status="OK")}
    with patch.object(step1, "_scrape_all", lambda today: (jobs, statuses, {"djinni.co": "перша_сторінка"})), \
         patch.object(step1, "call_json", lambda prompt, **kw: {"evaluations": [_ev(0)]}), \
         patch.object(step1, "fetch_full_text", lambda job: "Повністю віддалено. We welcome people with disabilities."), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)
    assert res["vacancies"][0].category == "інвалідність"


def test_reject_log_has_category_as_11th_column_after_date_source():
    assert len(REJECT_LOG_HEADER) == 13  # + «Вердикт Haiku», «Вердикт Sonnet» (26.10.2026)
    assert REJECT_LOG_HEADER[9] == "Джерело дати" and REJECT_LOG_HEADER[10] == "Категорія"
    res = _run([_job(0, "ЛОВЗ")], [_ev(0, veteran=True)])
    row = build_rows(res["decisions"], TODAY)[0]
    assert len(row) == 13 and row[10] == "обидві" and row[5] == "показано" and row[6] == "—"


def test_near_miss_gets_category():
    ev = {**_ev(0, passes=False, code="ЗП", veteran=True), "near_miss": True, "near_miss_reason": "ЗП 28 000 грн"}
    res = _run([_job(0, "ЛОВЗ")], [ev])
    assert res["near_misses"][0].category == "обидві"
    d = res["decisions"][0]
    assert d["result"] == "майже" and d["category"] == "обидві"


def test_prompt_lists_keywords_and_states_filters_unaffected():
    static, _ = build_vacancy_eval_parts([_job(0)], today=TODAY, stage="full")
    assert ", ".join(DISABILITY_KEYWORDS) in static
    assert "НЕ впливають на passes_criteria" in static

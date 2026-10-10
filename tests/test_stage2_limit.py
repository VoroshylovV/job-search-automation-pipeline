"""Етап 2 (10.10.2026): повний текст лише для High/Medium з етапу 1 і не більше
MAX_FULLTEXT_VACANCIES (10); решта — відсів (Low) або «не оцінено (ліміт етапу 2)»."""
from __future__ import annotations

import importlib
import json
from datetime import date
from unittest.mock import patch

import pytest

import pipeline.step1_vacancies as step1
from claude_orchestrator import cost
from models import RawJobPosting, SourceStatus

TODAY = date(2026, 10, 10)
_REAL_STAGE2 = importlib.import_module("pipeline.step1_vacancies").__dict__.get("_confirm_with_full_text")


@pytest.fixture(autouse=True)
def _fresh_tracker():
    cost.tracker.reset()
    yield
    cost.tracker.reset()


def _job(i):
    return RawJobPosting(source="robota.ua", title=f"Data Analyst {i}", company=f"C{i}",
                         url=f"https://robota.ua/company1/vacancy{1000 + i}", posted_raw="", salary_raw="",
                         description_snippet="Київ | короткий опис")


def _ev(i, level="Medium"):
    return {"raw_index": i, "passes_criteria": True, "reject_reason": None, "reject_code": None,
            "normalized_title": f"Data Analyst {i}", "normalized_company": f"C{i}",
            "match_level": level, "match_reasoning": "ok", "veteran_bonus": False,
            "low_match_location": False, "low_match_location_reason": None,
            "posted_date": TODAY.isoformat(), "date_undetermined": False}


def _run(levels, monkeypatch):
    """levels[i] — match_level вакансії i на етапі 1. Повертає (res, назви вакансій, що дійшли до етапу 2)."""
    jobs = [_job(i) for i in range(len(levels))]
    stage2_titles = []

    def fake(prompt, **kw):
        if "ЕТАП 2 з 2" in kw["cache_prefix"]:
            payload = json.loads(prompt[prompt.index("Вакансії:\n") + len("Вакансії:\n"):])
            stage2_titles.extend(p["title"] for p in payload)
            return {"evaluations": [_ev(k) for k in range(len(payload))]}
        return {"evaluations": [_ev(i, levels[i]) for i in range(len(jobs))]}

    monkeypatch.setattr(step1, "_confirm_with_full_text", _REAL_STAGE2)
    with patch.object(step1, "_scrape_all", lambda today: (jobs, {"robota.ua": SourceStatus("robota.ua")}, {"robota.ua": "перша_сторінка"})), \
         patch.object(step1, "call_json", fake), \
         patch.object(step1, "fetch_full_text", lambda job: "Повністю віддалено. " * 20), \
         patch.object(step1, "CLAUDE_EVAL_CHUNK_SIZE", 100), patch.object(step1, "CLAUDE_FULL_EVAL_CHUNK_SIZE", 100), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)
    return res, stage2_titles


def test_stage2_receives_only_high_and_medium(monkeypatch):
    res, titles = _run(["High", "Low", "Medium", "Low"], monkeypatch)
    assert sorted(titles) == ["Data Analyst 0", "Data Analyst 2"]
    by_id = {d["url"].rsplit("vacancy", 1)[1]: d for d in res["decisions"]}
    for low in ("1001", "1003"):
        assert by_id[low]["result"] == "відсіяно" and "Low на етапі 1" in by_id[low]["detail"]
    assert len(res["vacancies"]) == 2


def test_stage2_capped_at_10_best_first(monkeypatch):
    res, titles = _run(["Medium"] * 8 + ["High"] * 4, monkeypatch)  # 12 кандидатів
    assert len(titles) == 10
    assert {f"Data Analyst {i}" for i in range(8, 12)} <= set(titles)  # усі High пройшли
    assert res["stage2_capped_count"] == 2
    capped = [d for d in res["decisions"] if d["result"] == step1.EVAL_STAGE2_CAP_RESULT]
    assert len(capped) == 2 and all(d["code"] == "ліміт_етапу_2" for d in capped)
    assert len(res["vacancies"]) == 10 and len(res["decisions"]) == 12


def test_stage2_exactly_10_is_not_capped(monkeypatch):
    res, titles = _run(["Medium"] * 10, monkeypatch)
    assert len(titles) == 10 and res["stage2_capped_count"] == 0


def test_limit_value_is_10():
    import config
    assert config.MAX_FULLTEXT_VACANCIES == 10


def test_selfcheck_reports_stage2_cap():
    from pipeline.step4_selfcheck import _step1_status
    sources = {n: SourceStatus(n) for n in ["djinni.co", "jobs.dou.ua", "robota.ua", "work.ua", "happymonday.ua"]}
    status, note = _step1_status({"source_statuses": sources, "dedup_level_used": 1, "stage2_capped_count": 3})
    assert status == "ЧАСТКОВО" and "3 вакансій не оцінено (ліміт етапу 2)" in note

"""_evaluate_batch приймає відповідь моделі і як {"evaluations": [...]}, і як список оцінок;
інший тип — лог «невалідний формат відповіді (<тип>)», батч неоцінений, Крок 1 не падає."""
from __future__ import annotations

import logging
from datetime import date
from unittest.mock import patch

import pytest

import pipeline.step1_vacancies as step1
from models import RawJobPosting, SourceStatus

TODAY = date(2026, 10, 26)


def _job(i):
    return RawJobPosting(source="djinni.co", title=f"Data Analyst {i}", company=f"C{i}", url=f"https://djinni.co/jobs/{i}-x/",
                         posted_raw="", salary_raw="", description_snippet="remote")


def _ev(i):
    return {"raw_index": i, "passes_criteria": True, "reject_code": None, "normalized_title": f"Data Analyst {i}",
            "normalized_company": f"C{i}", "match_level": "Medium", "match_scores": {"skills": 1, "duties": 1, "expectations": 1},
            "match_reasoning": "ok", "veteran_bonus": False, "low_match_location": False, "low_match_location_reason": None,
            "posted_date": TODAY.isoformat(), "date_undetermined": False}


def _batch(response, monkeypatch, n=2):
    monkeypatch.setattr(step1, "call_json", lambda prompt, **kw: response)
    return step1._evaluate_batch([_job(i) for i in range(n)], TODAY, "card", {})


def test_dict_and_list_formats_give_identical_result(monkeypatch):
    evs = [_ev(0), _ev(1)]
    as_dict = _batch({"evaluations": evs}, monkeypatch)
    as_list = _batch(list(evs), monkeypatch)
    assert as_dict == as_list == {0: evs[0], 1: evs[1]}


def test_dict_without_evaluations_still_empty_as_before(monkeypatch):
    assert _batch({}, monkeypatch) == {}  # як і раніше: порожній результат, не помилка


@pytest.mark.parametrize("response,type_name", [("текст", "str"), (42, "int"), (None, "NoneType"), (3.5, "float"), (True, "bool")])
def test_invalid_type_is_logged_and_batch_unevaluated(monkeypatch, caplog, response, type_name):
    with caplog.at_level(logging.ERROR):
        assert _batch(response, monkeypatch) is None
    assert f"невалідний формат відповіді ({type_name})" in caplog.text


def test_dict_with_non_list_evaluations_is_invalid(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR):
        assert _batch({"evaluations": "ні"}, monkeypatch) is None
    assert "невалідний формат відповіді (dict.evaluations: str)" in caplog.text


def test_non_dict_items_skipped_rest_of_batch_kept(monkeypatch):
    got = _batch([_ev(0), "сміття", 7, None, _ev(1)], monkeypatch)
    assert set(got) == {0, 1}


def test_evaluate_marks_invalid_batch_as_failed_not_crash(monkeypatch):
    monkeypatch.setattr(step1, "call_json", lambda prompt, **kw: "не json-об'єкт")
    res = step1._evaluate([_job(0), _job(1)], TODAY, "card")
    assert all(ev.get("eval_failed") and ev["code"] == "помилка_Claude" for ev in res.values()) and len(res) == 2


def _run(response, monkeypatch):
    monkeypatch.setattr(step1, "call_json", lambda prompt, **kw: response)
    statuses = {"djinni.co": SourceStatus("djinni.co")}
    with patch.object(step1, "_scrape_all", lambda today: ([_job(0), _job(1)], statuses, {"djinni.co": "перша_сторінка"})), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        return step1.run_step1(utc_today=TODAY, local_today=TODAY)


def test_run_step1_same_result_for_both_formats_and_survives_invalid(monkeypatch):
    evs = [_ev(0), _ev(1)]
    a = _run({"evaluations": evs}, monkeypatch)
    b = _run(list(evs), monkeypatch)
    assert [v.url for v in a["vacancies"]] == [v.url for v in b["vacancies"]] and len(a["vacancies"]) == 2
    assert [d["result"] for d in a["decisions"]] == [d["result"] for d in b["decisions"]]
    bad = _run("не той тип", monkeypatch)  # не падає, обидві вакансії «не оцінено»
    assert bad["vacancies"] == [] and bad["unevaluated_count"] == 2
    assert {d["code"] for d in bad["decisions"]} == {"помилка_Claude"}

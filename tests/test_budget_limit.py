"""Запобіжник бюджету (10.10.2026): MAX_RUN_COST_USD → після ліміту решта
вакансій «не оцінено (ліміт бюджету)», без падіння; вартість у самоперевірці."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pipeline.step1_vacancies as step1
from claude_orchestrator import client, cost
from claude_orchestrator.client import ClaudeBudgetExceededError
from models import RawJobPosting, SourceStatus

TODAY = date(2026, 10, 10)


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


def _scrape(jobs):
    return lambda today: (jobs, {"robota.ua": SourceStatus("robota.ua")}, {"robota.ua": "перша_сторінка"})


def test_defaults():
    import config
    assert config.MAX_RUN_COST_USD == 0.30 and config.BUDGET_RESERVE_LATER_STEPS_USD == 0.05


def test_call_json_refuses_over_budget_without_calling_api(monkeypatch):
    seen = []
    monkeypatch.setattr(client, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: seen.append(kw))))
    cost.tracker.record("claude-sonnet-4-5", SimpleNamespace(input_tokens=0, output_tokens=20_000), "cards")  # $0.30
    with pytest.raises(ClaudeBudgetExceededError):
        client.call_json("x", budget_limit=0.25)
    assert seen == []


def test_call_json_without_limit_ignores_budget(monkeypatch):
    msg = SimpleNamespace(stop_reason="end_turn", usage=None, content=[SimpleNamespace(type="text", text="{}")])
    monkeypatch.setattr(client, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: msg)))
    cost.tracker.record("claude-sonnet-4-5", SimpleNamespace(input_tokens=0, output_tokens=20_000), "cards")
    assert client.call_json("x") == {}


def test_budget_limit_stops_calls_and_marks_rest_without_crash(monkeypatch):
    # $0.10 за виклик (haiku, 100k вхідних токенів); ліміт Кроку 1 = 0.30 - 0.05 = 0.25
    calls = []

    def create(**kw):
        text = kw["messages"][0]["content"][1]["text"]
        calls.append(kw["model"])
        n = text.count('"description_snippet": ')
        return SimpleNamespace(
            stop_reason="end_turn", usage=SimpleNamespace(input_tokens=100_000, output_tokens=0),
            content=[SimpleNamespace(type="text", text=json.dumps({"evaluations": [_ev(i) for i in range(n)]}))])

    monkeypatch.setattr(client, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    jobs = [_job(i) for i in range(30)]
    with patch.object(step1, "_scrape_all", _scrape(jobs)), \
         patch.object(step1, "CLAUDE_EVAL_CHUNK_SIZE", 5), \
         patch.object(step1, "MAX_RUN_COST_USD", 0.30), patch.object(step1, "BUDGET_RESERVE_LATER_STEPS_USD", 0.05), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)

    assert len(calls) == 3  # 3 × $0.10 = $0.30 ≥ $0.25 → решта батчів без виклику API
    assert cost.tracker.total_usd == pytest.approx(0.30)
    assert len(res["decisions"]) == 30
    budget = [d for d in res["decisions"] if d["result"] == step1.EVAL_BUDGET_RESULT]
    assert len(budget) == 15 and res["budget_unevaluated_count"] == 15
    assert all(d["code"] == "ліміт_бюджету" for d in budget)
    # 15 оцінених на етапі 1 → етап 2 бере 10 (ліміт), ще 5 — «ліміт етапу 2»
    assert len(res["vacancies"]) == 10 and res["stage2_capped_count"] == 5


def test_budget_exhausted_before_start_marks_everything_and_does_not_raise(monkeypatch):
    monkeypatch.setattr(client, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: pytest.fail("API не мав викликатись"))))
    cost.tracker.record("claude-sonnet-4-5", SimpleNamespace(input_tokens=0, output_tokens=20_000), "cards")
    with patch.object(step1, "_scrape_all", _scrape([_job(i) for i in range(4)])), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)
    assert res["vacancies"] == [] and res["budget_unevaluated_count"] == 4
    assert {d["result"] for d in res["decisions"]} == {step1.EVAL_BUDGET_RESULT}


def test_selfcheck_reports_budget_skips_and_cost(monkeypatch):
    from pipeline import step4_selfcheck as s4
    cost.tracker.record("claude-sonnet-4-5", SimpleNamespace(input_tokens=0, output_tokens=15_000), "cards")  # $0.225
    monkeypatch.setattr(s4, "_get_or_create_selfcheck_sheet", lambda: "sheet")
    monkeypatch.setattr(s4.sheets, "ensure_header", lambda *a, **k: None)
    monkeypatch.setattr(s4.sheets, "append_row", lambda *a, **k: None)
    monkeypatch.setattr(s4.sheets, "read_last_data_rows", lambda *a, **k: [])
    sources = {n: SourceStatus(n) for n in ["djinni.co", "jobs.dou.ua", "robota.ua", "work.ua", "happymonday.ua"]}
    r = s4.run_step4({"source_statuses": sources, "dedup_level_used": 1, "vacancies": [],
                      "total_found_before_filters": 10, "budget_unevaluated_count": 4, "collection_methods": {}},
                     {}, {"saved": True}, "2026-10-10 12:00 UTC")["result"]
    assert "Вартість Claude: $0.225 (ліміт $0.30" in r.notes
    assert r.step1_status == "ЧАСТКОВО" and "4 вакансій не оцінено (ліміт бюджету)" in r.step1_note
    assert not r.trend_comparable

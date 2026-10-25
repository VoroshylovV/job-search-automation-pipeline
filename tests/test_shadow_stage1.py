"""Паралельний етап 1 на двох моделях (з прогону 26.10.2026): Sonnet 5.5 — основна гілка,
Haiku 5.5 — тінь. Тінь лише пишеться в лог/метрики й НЕ впливає на вивід, відбір,
етап 2 і ліміти. Етап 2 і решта викликів моделі — Sonnet 4.5."""
from __future__ import annotations

import importlib
from datetime import date
from unittest.mock import patch

import pytest

import pipeline.step1_vacancies as step1
from claude_orchestrator import cost
from models import REJECT_LOG_HEADER, RawJobPosting, RunMetrics, SourceStatus
from pipeline.reject_log import build_rows
from pipeline.step4_selfcheck import shadow_summary

TODAY = date(2026, 10, 26)
SONNET, HAIKU, FULL = "claude-sonnet-5-5", "claude-haiku-5-5", "claude-sonnet-4-5"
_REAL_STAGE2 = importlib.import_module("pipeline.step1_vacancies").__dict__.get("_confirm_with_full_text")


@pytest.fixture(autouse=True)
def _models(monkeypatch):
    cost.tracker.reset()
    monkeypatch.setattr(step1, "CLAUDE_MODEL_CARDS", SONNET)
    monkeypatch.setattr(step1, "CLAUDE_MODEL_CARDS_SHADOW", HAIKU)
    monkeypatch.setattr(step1, "CLAUDE_MODEL_FULLTEXT", FULL)
    yield
    cost.tracker.reset()


def _job(i: int) -> RawJobPosting:
    return RawJobPosting(source="djinni.co", title=f"Data Analyst {i}", company=f"Company {i}",
                         url=f"https://djinni.co/jobs/{i}-x/", posted_raw="", salary_raw="", description_snippet="remote")


def _ev(i, *, passes=True, level="Medium", code="ЗП", reason="ЗП нижче порогу"):
    return {
        "raw_index": i, "passes_criteria": passes, "reject_code": None if passes else code,
        "reject_reason": None if passes else reason, "normalized_title": f"Data Analyst {i}",
        "normalized_company": f"Company {i}", "match_level": level if passes else None,
        "match_scores": {"skills": 1, "duties": 1, "expectations": 1} if passes else None,
        "match_reasoning": "ok", "veteran_bonus": False, "low_match_location": False,
        "low_match_location_reason": None, "posted_date": TODAY.isoformat(), "date_undetermined": False,
    }


def _run(jobs, by_model, monkeypatch, calls=None, full_text=lambda j: "Повністю віддалено. " * 20):
    """by_model: {модель: [evals]}; етап 2 (модель FULL) повертає evals Sonnet-гілки."""
    monkeypatch.setattr(step1, "_confirm_with_full_text", _REAL_STAGE2)
    scrapes = []

    def scrape(today):
        scrapes.append(1)
        return jobs, {"djinni.co": SourceStatus("djinni.co")}, {"djinni.co": "перша_сторінка"}

    def fake(prompt, **kw):
        if calls is not None:
            calls.append((kw["stage"], kw["model"], prompt, kw.get("cache_prefix")))
        evals = by_model.get(kw["model"])
        if evals is None:
            raise RuntimeError("модель недоступна")
        return {"evaluations": evals}

    with patch.object(step1, "_scrape_all", scrape), patch.object(step1, "call_json", fake), \
         patch.object(step1, "fetch_full_text", full_text), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)
    assert len(scrapes) == 1  # скрейпінг карток — один раз для обох гілок
    return res


def test_same_cards_same_prompt_both_models_and_stage2_on_sonnet_45(monkeypatch):
    calls = []
    full = [_ev(0)]
    res = _run([_job(0)], {SONNET: [_ev(0)], HAIKU: [_ev(0)], FULL: full}, monkeypatch, calls)
    cards = [c for c in calls if c[0] in ("cards", "cards_shadow")]
    assert [(c[0], c[1]) for c in cards] == [("cards", SONNET), ("cards_shadow", HAIKU)]
    assert cards[0][2] == cards[1][2] and cards[0][3] == cards[1][3]  # однаковий промпт (статика й динаміка)
    assert [(c[0], c[1]) for c in calls if c[0] == "fulltext"] == [("fulltext", FULL)]
    assert len(res["vacancies"]) == 1


def test_sonnet_passes_haiku_rejects_vacancy_is_shown(monkeypatch):
    haiku = [_ev(0, passes=False)]
    res = _run([_job(0)], {SONNET: [_ev(0)], HAIKU: haiku, FULL: [_ev(0)]}, monkeypatch)
    assert len(res["vacancies"]) == 1
    d = res["decisions"][0]
    assert d["result"] == "показано"
    assert d["verdict_sonnet"] == "пройшла · Match Medium"
    assert d["verdict_haiku"] == "відсіяно · ЗП: ЗП нижче порогу"
    sh = res["shadow"]
    assert (sh["compared"], sh["agree"], sh["disagree"]) == (1, 0, 1)
    assert sh["sonnet_pass_haiku_reject"] == 1 and sh["haiku_pass_sonnet_reject"] == 0


def test_haiku_passes_sonnet_rejects_not_shown_and_no_stage2(monkeypatch):
    fetched = []
    res = _run([_job(0)], {SONNET: [_ev(0, passes=False)], HAIKU: [_ev(0)]}, monkeypatch,
               full_text=lambda j: fetched.append(j) or "текст")
    assert res["vacancies"] == [] and fetched == []  # вердикт Haiku не дає пройти на етап 2
    d = res["decisions"][0]
    assert d["result"] == "відсіяно" and d["verdict_haiku"] == "пройшла · Match Medium"
    assert d["verdict_sonnet"].startswith("відсіяно · ЗП")
    assert res["shadow"]["haiku_pass_sonnet_reject"] == 1


def test_stage1_low_counts_as_reject_in_verdict_and_comparison(monkeypatch):
    low = _ev(0, level="Low")
    low["match_scores"] = {"skills": 0, "duties": 1, "expectations": 1}
    res = _run([_job(0)], {SONNET: [_ev(0)], HAIKU: [low], FULL: [_ev(0)]}, monkeypatch)
    assert res["decisions"][0]["verdict_haiku"] == "відсіяно · Low на етапі 1 (картка) · Match Low"
    assert res["shadow"]["sonnet_pass_haiku_reject"] == 1


def test_agreement_counts_and_log_columns_at_end(monkeypatch):
    jobs = [_job(i) for i in range(3)]
    sonnet = [_ev(0), _ev(1, passes=False), _ev(2)]
    haiku = [_ev(0), _ev(1, passes=False), _ev(2, passes=False)]
    res = _run(jobs, {SONNET: sonnet, HAIKU: haiku, FULL: [_ev(0), _ev(1)]}, monkeypatch)
    sh = res["shadow"]
    assert (sh["compared"], sh["agree"], sh["disagree"]) == (3, 2, 1)
    assert REJECT_LOG_HEADER[-3:] == ["Категорія", "Вердикт Haiku", "Вердикт Sonnet"]
    rows = build_rows(res["decisions"], TODAY)
    assert all(len(r) == len(REJECT_LOG_HEADER) for r in rows)
    assert rows[0][11] == "пройшла · Match Medium" and rows[0][12] == "пройшла · Match Medium"


def test_cards_rejected_before_stage1_have_empty_verdicts(monkeypatch):
    senior = RawJobPosting(source="djinni.co", title="Senior Data Analyst", company="X", url="https://djinni.co/jobs/9-x/",
                           posted_raw="", salary_raw="", description_snippet="")
    res = _run([senior, _job(0)], {SONNET: [_ev(0)], HAIKU: [_ev(0)], FULL: [_ev(0)]}, monkeypatch)
    by_code = {d["code"]: d for d in res["decisions"]}
    assert by_code["досвід"]["verdict_haiku"] == by_code["досвід"]["verdict_sonnet"] == ""


def test_shadow_failure_does_not_affect_output(monkeypatch):
    res = _run([_job(0)], {SONNET: [_ev(0)], FULL: [_ev(0)]}, monkeypatch)  # Haiku кидає виняток
    assert len(res["vacancies"]) == 1
    assert res["decisions"][0]["verdict_haiku"].startswith("не оцінено")
    assert res["shadow"]["compared"] == 0 and res["shadow"]["not_compared"] == 1


def test_shadow_disabled_makes_no_extra_calls(monkeypatch):
    monkeypatch.setattr(step1, "CLAUDE_MODEL_CARDS_SHADOW", "")
    calls = []
    res = _run([_job(0)], {SONNET: [_ev(0)], FULL: [_ev(0)]}, monkeypatch, calls)
    assert [c[1] for c in calls] == [SONNET, FULL]
    assert res["shadow"]["enabled"] is False and res["decisions"][0]["verdict_haiku"] == ""


def test_shadow_cost_counted_in_total_but_not_in_budget_limit():
    t = cost.tracker
    t.record(SONNET, {"input_tokens": 1_000_000, "output_tokens": 0}, "cards")          # $3
    t.record(HAIKU, {"input_tokens": 10_000_000, "output_tokens": 0}, "cards_shadow")   # $10
    assert t.total_usd == pytest.approx(13.0) and t.budget_usd == pytest.approx(3.0)
    assert not t.exceeded(5.0) and t.exceeded(3.0)


def test_metrics_and_selfcheck_report_disagreements(monkeypatch):
    res = _run([_job(0), _job(1)], {SONNET: [_ev(0), _ev(1, passes=False)], HAIKU: [_ev(0, passes=False), _ev(1)],
                                    FULL: [_ev(0)]}, monkeypatch)
    sh = res["shadow"]
    m = RunMetrics(run_date="d", model_cards_shadow="Haiku 5.5", shadow_compared=sh["compared"], shadow_agree=sh["agree"],
                   shadow_disagree=sh["disagree"], shadow_sonnet_pass_haiku_reject=sh["sonnet_pass_haiku_reject"],
                   shadow_haiku_pass_sonnet_reject=sh["haiku_pass_sonnet_reject"])
    assert m.as_row()[-6:] == ["Haiku 5.5", 2, 0, 2, 1, 1]
    text = shadow_summary(res)
    assert "збіглись 0 / розійшлись 2" in text and "Sonnet показав, Haiku відсіяв: 1" in text
    assert "Haiku показав, Sonnet відсіяв: 1" in text
    assert shadow_summary({}) == "тінь вимкнено"

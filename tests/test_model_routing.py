"""Окремі моделі за етапами (10.10.2026): картки — дешева модель (Haiku),
повні тексти й пошта — CLAUDE_MODEL_FULLTEXT."""
from __future__ import annotations

import importlib
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pipeline.step1_vacancies as step1
from claude_orchestrator import client, cost
from models import RawJobPosting, SourceStatus

TODAY = date(2026, 10, 10)
_REAL_STAGE2 = importlib.import_module("pipeline.step1_vacancies").__dict__.get("_confirm_with_full_text")


@pytest.fixture(autouse=True)
def _fresh_tracker():
    cost.tracker.reset()
    yield
    cost.tracker.reset()


def test_config_defaults_haiku_for_cards_and_main_model_for_fulltext():
    import config
    assert "haiku" in config.CLAUDE_MODEL_CARDS
    assert config.CLAUDE_MODEL_FULLTEXT == config.CLAUDE_MODEL


def test_call_json_uses_given_model_and_bills_by_it(monkeypatch):
    seen = []

    def create(**kw):
        seen.append(kw["model"])
        return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0),
                               content=[SimpleNamespace(type="text", text="{}")])

    monkeypatch.setattr(client, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    client.call_json("p", stage="cards", model="claude-haiku-4-5-20251001")
    client.call_json("p", stage="fulltext", model="claude-sonnet-4-5-20250929")
    assert seen == ["claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"]
    assert cost.tracker.stage("cards").usd == pytest.approx(1.0)      # Haiku $1/Mтокенів
    assert cost.tracker.stage("fulltext").usd == pytest.approx(3.0)   # Sonnet $3/Mтокенів


def test_step1_routes_models_by_stage(monkeypatch):
    used = []

    def fake(prompt, **kw):
        used.append((kw["stage"], kw["model"]))
        n = prompt.count('"description_snippet": ')
        return {"evaluations": [{"raw_index": i, "passes_criteria": True, "reject_code": None,
                                 "normalized_title": "Data Analyst", "normalized_company": "C",
                                 "match_level": "Medium", "match_reasoning": "ok", "veteran_bonus": False,
                                 "low_match_location": False, "low_match_location_reason": None,
                                 "posted_date": TODAY.isoformat(), "date_undetermined": False} for i in range(n)]}

    job = RawJobPosting(source="robota.ua", title="Data Analyst", company="C", url="https://robota.ua/company1/vacancy1",
                        posted_raw="", salary_raw="", description_snippet="Київ")
    # справжній етап 2 (conftest підміняє його «прозорим»)
    monkeypatch.setattr(step1, "_confirm_with_full_text", _REAL_STAGE2)
    with patch.object(step1, "_scrape_all", lambda today: ([job], {"robota.ua": SourceStatus("robota.ua")}, {"robota.ua": "перша_сторінка"})), \
         patch.object(step1, "call_json", fake), \
         patch.object(step1, "fetch_full_text", lambda j: "Повністю віддалено. " * 20), \
         patch.object(step1, "CLAUDE_MODEL_CARDS", "CARDS-MODEL"), patch.object(step1, "CLAUDE_MODEL_FULLTEXT", "FULL-MODEL"), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        step1.run_step1(utc_today=TODAY, local_today=TODAY)
    assert used == [("cards", "CARDS-MODEL"), ("fulltext", "FULL-MODEL")]


def test_step2_uses_fulltext_model(monkeypatch):
    from pipeline import step2_mail
    used = []
    monkeypatch.setattr(step2_mail, "CLAUDE_MODEL_FULLTEXT", "FULL-MODEL")
    monkeypatch.setattr(step2_mail, "call_json", lambda prompt, **kw: used.append((kw["stage"], kw["model"])) or {"evaluations": []})
    monkeypatch.setattr(step2_mail.time, "sleep", lambda s: None)
    monkeypatch.setattr(step2_mail.gmail, "get_or_create_label", lambda n: "L")
    monkeypatch.setattr(step2_mail.gmail, "search_threads", lambda c, label_id_to_exclude: [{"id": "a"}])
    msg = {"id": "m", "payload": {"headers": [{"name": "From", "value": "hr@x.com"}, {"name": "Subject", "value": "s"},
                                               {"name": "Date", "value": "d"}], "mimeType": "text/plain", "body": {}}, "snippet": "тіло"}
    monkeypatch.setattr(step2_mail.gmail, "get_thread", lambda tid: {"messages": [msg]})
    step2_mail.run_step2(company_names=[])
    assert used == [("mail", "FULL-MODEL")]

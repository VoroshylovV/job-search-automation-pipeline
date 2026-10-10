"""Prompt caching (10.10.2026): статична частина (профіль + правила) — окремий
блок з cache_control, динамічна — окремо; вартість враховує запис/читання кешу."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from claude_orchestrator import client, cost
from claude_orchestrator.prompts import (
    build_email_classify_parts,
    build_email_classify_prompt,
    build_vacancy_eval_parts,
    build_vacancy_eval_prompt,
)
from models import RawJobPosting

TODAY = date(2026, 10, 10)


@pytest.fixture(autouse=True)
def _fresh_tracker():
    cost.tracker.reset()
    yield
    cost.tracker.reset()


def _job(i):
    return RawJobPosting(source="robota.ua", title=f"Data Analyst {i}", company=f"C{i}",
                         url=f"https://robota.ua/company1/vacancy{1000 + i}", posted_raw="", salary_raw="",
                         description_snippet="Київ")


def test_vacancy_parts_join_to_full_prompt_and_static_is_batch_independent():
    a, b = [_job(0)], [_job(1), _job(2)]
    s1, d1 = build_vacancy_eval_parts(a, today=TODAY)
    s2, d2 = build_vacancy_eval_parts(b, today=TODAY)
    assert s1 == s2  # однакова статика для різних батчів — саме її кешуємо
    assert s1 + d1 == build_vacancy_eval_prompt(a, today=TODAY)
    assert "vacancy1000" in d1 and "vacancy1000" not in s1 and "Сьогоднішня дата" in d1


def test_static_part_differs_between_stages_but_not_within_stage():
    card, _ = build_vacancy_eval_parts([_job(0)], today=TODAY, stage="card")
    full, _ = build_vacancy_eval_parts([_job(0)], today=TODAY, stage="full", full_texts={0: "текст"})
    assert card != full and "ЕТАП 1 з 2" in card and "ЕТАП 2 з 2" in full


def test_email_parts_join_to_full_prompt():
    emails = [{"sender": "hr@x.com", "subject": "s", "date": "d", "body_text": "тіло"}]
    static, dynamic = build_email_classify_parts(emails)
    assert static + dynamic == build_email_classify_prompt(emails)
    assert "hr@x.com" in dynamic and "hr@x.com" not in static


def _fake_client(monkeypatch, usage, seen):
    def create(**kw):
        seen.append(kw)
        return SimpleNamespace(stop_reason="end_turn", usage=usage, content=[SimpleNamespace(type="text", text="{}")])
    monkeypatch.setattr(client, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))


def test_call_json_sends_cached_static_block_then_dynamic(monkeypatch):
    seen = []
    _fake_client(monkeypatch, SimpleNamespace(input_tokens=10, output_tokens=1), seen)
    client.call_json("динаміка", cache_prefix="СТАТИКА")
    blocks = seen[0]["messages"][0]["content"]
    assert blocks == [
        {"type": "text", "text": "СТАТИКА", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "динаміка"},
    ]


def test_call_json_without_prefix_keeps_plain_string(monkeypatch):
    seen = []
    _fake_client(monkeypatch, SimpleNamespace(input_tokens=1, output_tokens=1), seen)
    client.call_json("просто текст")
    assert seen[0]["messages"][0]["content"] == "просто текст"


def test_cache_read_is_ten_times_cheaper_than_plain_input(monkeypatch):
    seen = []
    plain = SimpleNamespace(input_tokens=100_000, output_tokens=0)
    cached = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=100_000, cache_creation_input_tokens=0)
    _fake_client(monkeypatch, plain, seen)
    client.call_json("p", stage="cards", model="claude-sonnet-4-5")
    _fake_client(monkeypatch, cached, seen)
    client.call_json("p", stage="fulltext", model="claude-sonnet-4-5")
    assert cost.tracker.stage("fulltext").usd == pytest.approx(cost.tracker.stage("cards").usd / 10)
    assert cost.tracker.stage("fulltext").cache_read_tokens == 100_000

"""Облік вартості (10.10.2026): usage → $, розкладка по етапах, logs/costs.csv,
колонки метрик і підсумок у самоперевірці. Без мережі."""
from __future__ import annotations

import csv
from types import SimpleNamespace

import pytest

from claude_orchestrator import client, cost
from models import METRICS_HEADER, RunMetrics, SourceStatus
from pipeline import cost_log
from pipeline import step4_selfcheck as s4


@pytest.fixture(autouse=True)
def _fresh_tracker():
    cost.tracker.reset()
    yield
    cost.tracker.reset()


def _usage(i=0, o=0, cr=0, cw=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


def test_usage_cost_all_four_token_kinds():
    u = _usage(1_000_000, 100_000, cr=1_000_000, cw=100_000)
    # haiku: 1.0 + 0.5 + 0.1 + 0.125 = 1.725
    assert cost.usage_cost("claude-haiku-4-5", u) == pytest.approx(1.725)
    # sonnet: 3.0 + 1.5 + 0.3 + 0.375 = 5.175
    assert cost.usage_cost("claude-sonnet-4-5", u) == pytest.approx(5.175)


def test_unknown_model_priced_as_most_expensive_family():
    assert cost.usage_cost("some-new-model", _usage(1_000_000)) == pytest.approx(5.0)


def test_tracker_splits_by_stage_and_sums():
    cost.tracker.record("claude-sonnet-4-5", _usage(1000, 500), "cards")
    cost.tracker.record("claude-sonnet-4-5", _usage(2000, 0, cr=4000, cw=1000), "fulltext")
    cost.tracker.record("claude-sonnet-4-5", _usage(100, 100), "mail")
    c, f = cost.tracker.stage("cards"), cost.tracker.stage("fulltext")
    assert (c.calls, c.input_tokens, c.output_tokens) == (1, 1000, 500)
    assert (f.cache_read_tokens, f.cache_write_tokens) == (4000, 1000)
    assert cost.tracker.stage("freelance").usd == 0
    assert cost.tracker.total_usd == pytest.approx(c.usd + f.usd + cost.tracker.stage("mail").usd)
    assert cost.tracker.calls == 3


def test_call_json_records_usage_under_stage(monkeypatch):
    msg = SimpleNamespace(stop_reason="end_turn", usage=_usage(2000, 1000),
                          content=[SimpleNamespace(type="text", text="{}")])
    monkeypatch.setattr(client, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: msg)))
    client.call_json("p", stage="freelance")
    assert cost.tracker.stage("freelance").calls == 1 and cost.tracker.stage("cards").calls == 0


def _selfcheck(**kw):
    base = dict(step1_status="OK", step2_status="OK", step3_metrics_status="OK", step1_2_status="OK")
    base.update(kw)
    return SimpleNamespace(**base)


def _vac(level):
    return SimpleNamespace(match_level=level)


def test_costs_csv_appends_and_writes_header_once(tmp_path):
    cost.tracker.record("claude-sonnet-4-5", _usage(1000, 1000), "cards")
    cost.tracker.record("claude-haiku-4-5", _usage(1000, 1000), "mail")
    step1 = {"vacancies": [_vac("High"), _vac("Low"), _vac("Low")], "total_found_before_filters": 123, "evaluated_count": 40}
    step5 = {"active": True, "row": SimpleNamespace(v_url_overlap=2)}
    path = tmp_path / "logs" / "costs.csv"
    for _ in range(2):
        cost_log.append_cost_row(cost_log.build_row("2026-10-11 12:00 UTC", step1, _selfcheck(), step5, commit="abc1234"), path)
    rows = list(csv.reader(open(path, encoding="utf-8")))
    assert len(rows) == 3 and rows[0] == cost_log.COSTS_HEADER  # заголовок один раз, рядки дописуються
    rec = dict(zip(cost_log.COSTS_HEADER, rows[1]))
    assert rec["commit"] == "abc1234" and rec["models"] == "cards=claude-sonnet-4-5;mail=claude-haiku-4-5"
    assert rec["cards_calls"] == "1" and rec["cards_in"] == "1000" and rec["fulltext_calls"] == "0"
    assert float(rec["total_usd"]) == pytest.approx(cost.tracker.total_usd, abs=1e-5)
    assert (rec["found"], rec["evaluated"], rec["shown"]) == ("123", "40", "3")
    assert float(rec["usd_per_shown"]) == pytest.approx(cost.tracker.total_usd / 3, abs=1e-5)
    assert (rec["shown_high"], rec["shown_medium"], rec["shown_low"]) == ("1", "0", "2")
    assert rec["url_overlap_online"] == "2" and rec["run_status"] == "OK"


def test_costs_csv_overlap_na_without_step5_and_status_partial(tmp_path):
    step1 = {"vacancies": [], "total_found_before_filters": 5}
    row = cost_log.build_row("t", step1, _selfcheck(step2_status="НЕ ВИКОНАНО"), {"active": False}, commit="x")
    rec = dict(zip(cost_log.COSTS_HEADER, row))
    assert rec["url_overlap_online"] == "н/д" and rec["usd_per_shown"] == "н/д" and rec["run_status"] == "ЧАСТКОВО"


def test_metrics_row_has_cost_columns_matching_header():
    m = RunMetrics(run_date="2026-10-11", cost_total_usd=0.31234, cost_per_shown_usd=0.1562)
    row = m.as_row()
    assert len(row) == len(METRICS_HEADER)
    assert METRICS_HEADER[-2:] == ["Вартість Claude, $ разом", "Вартість Claude, $ на показану вакансію"]
    assert row[-2:] == [0.3123, 0.1562]
    assert RunMetrics(run_date="d").as_row()[-1] == "н/д"


def test_selfcheck_note_has_total_and_stage_breakdown():
    cost.tracker.record("claude-sonnet-4-5", _usage(1_000_000), "cards")      # $3
    cost.tracker.record("claude-sonnet-4-5", _usage(0, 100_000), "fulltext")  # $1.5
    note = s4.cost_note()
    assert "Вартість Claude: $4.500 (2 викликів)" in note
    assert "етап 1 (картки) $3.000" in note and "етап 2 (повні тексти) $1.500" in note
    assert "Крок 1.2 (фріланс) $0.000" in note and "Крок 2 (пошта) $0.000" in note


def test_selfcheck_result_and_block_include_cost(monkeypatch):
    cost.tracker.record("claude-sonnet-4-5", _usage(1_000_000), "cards")
    monkeypatch.setattr(s4, "_get_or_create_selfcheck_sheet", lambda: "sheet")
    monkeypatch.setattr(s4.sheets, "ensure_header", lambda *a, **k: None)
    monkeypatch.setattr(s4.sheets, "append_row", lambda *a, **k: None)
    monkeypatch.setattr(s4.sheets, "read_last_data_rows", lambda *a, **k: [])
    sources = {n: SourceStatus(n) for n in ["djinni.co", "jobs.dou.ua", "robota.ua", "work.ua", "happymonday.ua"]}
    r = s4.run_step4({"source_statuses": sources, "dedup_level_used": 1, "vacancies": [],
                      "total_found_before_filters": 10, "collection_methods": {}},
                     {}, {"saved": True}, "2026-10-11 12:00 UTC")["result"]
    assert "Вартість Claude: $3.000" in r.notes
    assert r.as_row()[10] == r.notes
    assert r.notes in s4.format_selfcheck_block(r)

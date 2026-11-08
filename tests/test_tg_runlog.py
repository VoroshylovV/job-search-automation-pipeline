"""tg_market: runs.csv, сирий текст постів, сталі схеми CSV, is_it для marketing (без мережі й API)."""
import csv
import json

import pytest

from tg_market import runlog
from tg_market.parse import Post


def _rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_ensure_schema_migrates_old_header_and_keeps_backup(tmp_path):
    p = tmp_path / "posts.csv"
    p.write_text("channel,post_id,datetime,views,classified,is_vacancy,vacancy_count,collected_at\n"
                 "a,1,2026-08-01T00:00:00+00:00,10,True,True,2,2026-11-01\n", encoding="utf-8")
    runlog.append_csv(p, runlog.POST_FIELDS, [{"run_id": "R1", "channel": "b", "post_id": 2, "datetime": "d",
                                               "views": 1, "classified": True, "is_vacancy": False,
                                               "vacancy_count": 0, "collected_at": "c"}])
    rows = _rows(p)
    assert list(rows[0].keys()) == runlog.POST_FIELDS
    assert rows[0]["run_id"] == "" and rows[0]["channel"] == "a"      # старий рядок збережено, нова колонка порожня
    assert rows[1]["run_id"] == "R1"
    assert (tmp_path / "posts.csv.bak").exists()


def test_ensure_schema_refuses_unknown_columns(tmp_path):
    p = tmp_path / "posts.csv"
    p.write_text("channel,post_id,mystery\na,1,x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        runlog.ensure_schema(p, runlog.POST_FIELDS)
    assert p.read_text(encoding="utf-8").startswith("channel,post_id,mystery")   # файл не зачеплено


def test_ensure_schema_refuses_rows_longer_than_header(tmp_path):
    p = tmp_path / "posts.csv"
    p.write_text("channel,post_id\na,1,EXTRA\n", encoding="utf-8")
    with pytest.raises(ValueError):
        runlog.ensure_schema(p, runlog.POST_FIELDS)


def test_save_raw_appends_once_per_post(tmp_path):
    posts = [Post("ch", 1, "2026-10-01T00:00:00+00:00", 5, "Текст 1"), Post("ch", 2, "2026-10-02T00:00:00+00:00", None, "Текст 2")]
    assert runlog.save_raw(tmp_path / "raw", "R1", posts) == 2
    assert runlog.save_raw(tmp_path / "raw", "R2", posts + [Post("ch", 3, "2026-10-03T00:00:00+00:00", 1, "Текст 3")]) == 1
    lines = [json.loads(x) for x in (tmp_path / "raw" / "ch.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["post_id"] for x in lines] == [1, 2, 3]
    assert lines[0]["text"] == "Текст 1" and lines[2]["run_id"] == "R2"


def test_build_run_row_cost_per_post_and_source():
    row = runlog.build_run_row(
        run_id="R1", started_at="s", since="2026-08-01", panel="core", channels=["a", "b"], model="m",
        prompt_text="prompt", posts=200, classified=198, vacancy_posts=120, vacancies=150,
        tokens={"calls": 20, "input": 1000, "output": 500, "cache_read": 0, "cache_write": 0},
        cost_usd=1.0, status="ok", git=("abc1234", False))
    assert list(row) == runlog.RUN_FIELDS
    assert row["source"] == "telegram" and row["channels"] == "a; b"
    assert row["cost_per_post_usd"] == 0.005 and row["cost_per_1000_posts_usd"] == 5.0
    assert row["prompt_hash"] == runlog.prompt_hash("prompt") != runlog.prompt_hash("prompt2")


def test_previous_since_reads_last_run(tmp_path):
    p = tmp_path / "runs.csv"
    assert runlog.previous_since(p) is None
    p.write_text("run_id,since\nR1,2026-08-01\nR2,2026-09-01\n", encoding="utf-8")
    assert runlog.previous_since(p) == "2026-09-01"


def test_marketing_and_non_it_are_never_is_it_at_write_time():
    from tg_market.classify import normalize_vacancy
    m = normalize_vacancy({"title": "SMM", "is_it": True, "direction": "marketing"})
    assert m["is_it"] is False and m["direction"] == "marketing"
    n = normalize_vacancy({"title": "HR", "is_it": True, "direction": "non-it"})
    assert n["is_it"] is False
    d = normalize_vacancy({"title": "QA", "is_it": True, "direction": "qa"})
    assert d["is_it"] is True


def test_analyze_fixes_legacy_marketing_flagged_as_it():
    from tg_market.analyze import reclassify
    u = {"title": "Performance marketer", "direction": "marketing", "is_it": "True"}
    assert reclassify(u) == "marketing/non-it is_it→False" and u["is_it"] == "False"
    ok = {"title": "QA Engineer", "direction": "qa", "is_it": "True"}
    assert reclassify(ok) is None


def test_collect_main_writes_runs_raw_and_posts(tmp_path, monkeypatch):
    import sys

    import tg_market.collect as col
    from claude_orchestrator.cost import tracker

    monkeypatch.setattr(col, "DATA", tmp_path)
    monkeypatch.setattr(col, "load_channels", lambda panel: [{"handle": "ch", "panel": "core", "max_pages": "1"}])
    monkeypatch.setattr(col, "get_session", lambda: object())
    posts = [Post("ch", 1, "2026-10-01T10:00:00+00:00", 7, "Junior Data Analyst, Acme"),
             Post("ch", 2, "2026-10-02T10:00:00+00:00", 9, "Реклама")]
    monkeypatch.setattr(col, "fetch_channel", lambda *a, **k: posts)

    def fake_call_json(prompt, max_tokens, model=None):
        tracker.record("claude-haiku-5-5", {"input_tokens": 1000, "output_tokens": 200}, "other")
        return {"posts": [
            {"key": "ch/1", "is_vacancy": True, "vacancies": [{"title": "Junior Data Analyst", "company": "Acme",
                                                               "is_it": True, "direction": "analytics", "level": "junior"}]},
            {"key": "ch/2", "is_vacancy": True, "vacancies": [{"title": "SMM", "company": "X", "is_it": True,
                                                               "direction": "marketing"}]}]}

    monkeypatch.setattr(col, "call_json", fake_call_json)
    monkeypatch.setattr(sys, "argv", ["collect", "--since", "2026-08-01"])
    col.main()

    runs = _rows(tmp_path / "runs.csv")
    assert len(runs) == 1 and runs[0]["source"] == "telegram" and runs[0]["since"] == "2026-08-01"
    assert runs[0]["posts"] == "2" and runs[0]["vacancies"] == "2" and runs[0]["status"] == "ok"
    assert runs[0]["input_tokens"] == "1000" and runs[0]["output_tokens"] == "200"
    assert float(runs[0]["cost_usd"]) > 0 and runs[0]["model"] == col.TG_MARKET_MODEL
    vac = _rows(tmp_path / "vacancies.csv")
    assert {r["run_id"] for r in vac} == {runs[0]["run_id"]}
    assert [r["is_it"] for r in vac] == ["True", "False"]              # marketing — не IT вже при записі
    raw = (tmp_path / "raw" / "ch.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(raw[0])["text"] == "Junior Data Analyst, Acme"


def test_collect_main_logs_failed_run_with_spent_tokens(tmp_path, monkeypatch):
    import sys

    import tg_market.collect as col
    from claude_orchestrator.cost import tracker

    monkeypatch.setattr(col, "DATA", tmp_path)
    monkeypatch.setattr(col, "load_channels", lambda panel: [{"handle": "ch", "panel": "core", "max_pages": "1"}])
    monkeypatch.setattr(col, "get_session", lambda: object())
    monkeypatch.setattr(col, "fetch_channel", lambda *a, **k: [Post("ch", 1, "2026-10-01T10:00:00+00:00", 1, "t")])

    def boom(prompt, max_tokens, model=None):
        tracker.record("claude-haiku-5-5", {"input_tokens": 500, "output_tokens": 0}, "other")
        raise RuntimeError("мережа впала")

    monkeypatch.setattr(col, "call_json", boom)
    monkeypatch.setattr(sys, "argv", ["collect", "--since", "2026-08-01"])
    with pytest.raises(RuntimeError):
        col.main()
    runs = _rows(tmp_path / "runs.csv")
    assert runs[0]["status"] == "failed" and runs[0]["input_tokens"] == "500"
    assert (tmp_path / "raw" / "ch.jsonl").exists()                    # текст збережено попри збій

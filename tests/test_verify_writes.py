"""verify_writes(): перевірка запису наприкінці запуску, одна спроба дозапису, тривога НЕ ЗАПИСАНО."""
from __future__ import annotations

import importlib
from datetime import date
from types import SimpleNamespace

import pytest

from config import DEDUP_LOG_TITLE, METRICS_KEY_COLUMNS, METRICS_SHEET_TITLE, SELFCHECK_SHEET_TITLE, SOURCES
from models import METRICS_HEADER, REJECT_LOG_HEADER, SourceStatus
from pipeline import alarm, reject_log, verify_writes as vw

TODAY = date(2026, 10, 26)
TS = "2026-10-26 11:00 UTC"
RL_TITLE = "Лог відсіву 2026-10-26 (тест гітхаб)"


@pytest.fixture(autouse=True)
def _clean_alarms():
    alarm.reset()
    yield
    alarm.reset()


class World:
    """Підроблений Drive: назва -> файл, значення таблиць, текст дедуп-логу, modifiedTime."""

    def __init__(self):
        self.files = {RL_TITLE: "rl", METRICS_SHEET_TITLE: "met", SELFCHECK_SHEET_TITLE: "sc", DEDUP_LOG_TITLE: "dd"}
        self.modified = {"rl": "2026-10-26T11:05:03.123Z"}
        data_rows = [["2026-10-26", "тест гітхаб", "djinni.co", f"u{i}", "x", "відсіяно", "ЗП", "", "", "", "—", "", ""] for i in range(10)]
        summary = reject_log.summary_row(TODAY, "знайдено 10")
        self.values = {
            "rl": [REJECT_LOG_HEADER] + data_rows + [summary],
            "met": [METRICS_HEADER, self.metrics_row(10, 1)],
            "sc": [["Перевірка"], [TS, "OK", "OK"]],
        }
        self.doc = "2026-10-20 | https://old | A — B\n2026-10-26 | https://x/1 | Data Analyst 1 — C1\n"

    @staticmethod
    def metrics_row(found, shown):
        row = [""] * len(METRICS_HEADER)
        for col in METRICS_KEY_COLUMNS:
            row[METRICS_HEADER.index(col)] = "OK"
        row[0], row[1], row[2] = "2026-10-26", str(found), str(shown)
        row[METRICS_HEADER.index("Вартість Claude, $ разом")] = "0.2"
        return row


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(vw.drive, "find_file_by_title", lambda t, f=None: {"id": w.files[t]} if t in w.files else None)
    monkeypatch.setattr(vw.drive, "get_modified_time", lambda fid: w.modified[fid])
    monkeypatch.setattr(vw.sheets, "read_values", lambda fid, rng="A1:ZZ": [list(r) for r in w.values[fid]])
    monkeypatch.setattr(vw.docs, "read_full_text", lambda fid: w.doc)
    monkeypatch.setattr(vw.docs, "replace_full_text", lambda fid, text: setattr(w, "doc", text))
    return w


def _ctx(**over):
    st = {s: SourceStatus(s, "OK", count=3) for s in SOURCES}
    vacancy = SimpleNamespace(url="https://x/1")
    step1 = {"total_found_before_filters": 10, "vacancies": [vacancy], "source_statuses": st,
             "shown_log_lines": ["2026-10-26 | https://x/1 | Data Analyst 1 — C1"]}
    base = dict(utc_today=TODAY, step1_result=step1, step3_result={"metrics": SimpleNamespace(as_row=lambda: World.metrics_row(10, 1))},
                step4_result={"result": SimpleNamespace(as_row=lambda: [TS, "OK"])}, selfcheck_timestamp=TS, start_marker={},
                reject_log={"title": RL_TITLE, "file_id": "rl", "values": [], "run_index": 0})
    base.update(over)
    return vw.VerifyContext(**base)


def test_all_good(world):
    res = vw.verify_writes(_ctx())
    assert res.ok and res.failed == [] and not alarm.active()


def test_reject_log_checks_and_repair(world, monkeypatch):
    world.modified["rl"] = "2026-10-25T23:00:00Z"  # не сьогодні

    def fix(ctx):
        world.modified["rl"] = "2026-10-26T11:10:00Z"
    monkeypatch.setattr(vw, "repair_reject_log", fix)
    monkeypatch.setattr(vw, "CHECKS", [(n, c, fix if n == "лог відсіву" else r) for n, c, r in vw.CHECKS])
    res = vw.verify_writes(_ctx())
    assert res.ok and res.repaired == ["лог відсіву"]


def test_reject_log_not_summary_or_too_few_rows_raises_alarm_first(world):
    alarm.add("якась інша тривога")
    world.values["rl"].pop()  # прибрали «ПІДСУМОК»
    res = vw.verify_writes(_ctx())
    assert not res.ok
    lines = alarm.format_block().splitlines()
    assert lines[0] == "🚨🚨🚨 !!! НЕ ЗАПИСАНО: ЛОГ ВІДСІВУ: ОСТАННІЙ РЯДОК НЕ «ПІДСУМОК» !!!" and "ІНША ТРИВОГА" in lines[-1]


def test_reject_log_rows_share_90_percent(world):
    ctx = _ctx()
    ctx.step1_result["total_found_before_filters"] = 11  # потрібно ≥ ceil(9.9) = 10 -> 10 рядків даних є
    assert vw.check_reject_log(ctx)[0]
    ctx.step1_result["total_found_before_filters"] = 12  # ≥ ceil(10.8) = 11 -> є лише 10
    ok, why = vw.check_reject_log(ctx)
    assert not ok and "10 з 12" in why


def test_reject_log_missing_file(world):
    del world.files[RL_TITLE]
    assert "відсутній у Drive" in vw.check_reject_log(_ctx())[1]
    assert "запис не виконувався" in vw.check_reject_log(_ctx(reject_log=None))[1]


def test_shown_log_missing_rows_repaired_by_append(world):
    world.doc = "2026-10-20 | https://old | A — B\n"  # за сьогодні 0 рядків, показано 1
    res = vw.verify_writes(_ctx())
    assert res.ok and "лог показаних" in res.repaired and "https://x/1" in world.doc


def test_shown_log_repeat_run_allows_more_lines(world):
    world.doc += "2026-10-26 | https://other | Z — Y\n"  # рядок раннього запуску того ж дня
    assert not vw.check_shown_log(_ctx())[0]
    assert vw.check_shown_log(_ctx(reject_log={"title": RL_TITLE, "file_id": "rl", "values": [], "run_index": 1}))[0]


def test_metrics_missing_key_column_repaired(world, monkeypatch):
    bad = World.metrics_row(10, 1)
    bad[METRICS_HEADER.index("Модель крок 1 (картки)")] = ""
    world.values["met"] = [METRICS_HEADER, bad]
    monkeypatch.setattr(vw.sheets, "ensure_header", lambda *a, **k: None)
    monkeypatch.setattr(vw.sheets, "upsert_row_by_first_cell", lambda sid, row: world.values.__setitem__("met", [METRICS_HEADER, row]))
    assert "Модель крок 1" in vw.check_metrics(_ctx())[1]
    res = vw.verify_writes(_ctx())
    assert res.ok and "метрики" in res.repaired


def test_metrics_no_row_today_alarm(world, monkeypatch):
    world.values["met"] = [METRICS_HEADER]
    monkeypatch.setattr(vw.sheets, "ensure_header", lambda *a, **k: None)
    monkeypatch.setattr(vw.sheets, "upsert_row_by_first_cell", lambda *a: "noop")  # дозапис «не спрацював»
    res = vw.verify_writes(_ctx())
    assert not res.ok and alarm.format_block().splitlines()[0] == "🚨🚨🚨 !!! НЕ ЗАПИСАНО: МЕТРИКИ: НЕМАЄ РЯДКА ЗА СЬОГОДНІ !!!"


def test_selfcheck_still_start_marker_repaired_in_place(world, monkeypatch):
    world.values["sc"] = [["Перевірка"], [f"{TS} — ЗАПУСК ПОЧАТО"]]
    calls = []
    monkeypatch.setattr(vw.sheets, "update_row", lambda sid, n, row: (calls.append(n), world.values.__setitem__("sc", [["Перевірка"], row]))[0])
    res = vw.verify_writes(_ctx(start_marker={"sheet_id": "sc", "row": 2}))
    assert res.ok and calls == [2] and "результат перевірки" in res.repaired


def test_sources_not_read_no_repair_and_alarm(world):
    ctx = _ctx()
    ctx.step1_result["source_statuses"]["work.ua"] = SourceStatus("work.ua", "недоступне", "HTTP 403")
    del ctx.step1_result["source_statuses"]["happymonday.ua"]
    res = vw.verify_writes(ctx)
    assert not res.ok and "work.ua, happymonday.ua" in res.failed[0]
    assert alarm.format_block().startswith("🚨🚨🚨 !!! НЕ ЗАПИСАНО: ДЖЕРЕЛА НЕ ПРОЧИТАНО")


def test_check_exception_counts_as_not_verified(world, monkeypatch):
    monkeypatch.setattr(vw.drive, "get_modified_time", lambda fid: (_ for _ in ()).throw(RuntimeError("api")))
    res = vw.verify_writes(_ctx())
    assert not res.ok and "не вдалось перевірити" in res.failed[0]


def test_summary_row_and_reject_log_summary_text():
    row = reject_log.summary_row(TODAY, "знайдено 5")
    assert len(row) == len(REJECT_LOG_HEADER) and row[5] == "ПІДСУМОК" and row[7] == "знайдено 5"
    main = importlib.import_module("main")
    text = main._reject_log_summary({"total_found_before_filters": 5, "vacancies": [1],
                                     "decisions": [{"result": "показано"}, {"result": "відсіяно"}, {"result": "майже"},
                                                   {"result": "не оцінено (помилка Claude)"}]})
    assert text == "знайдено 5; показано 1; відсіяно 1; майже 1; не оцінено 1; рядків 4"


def test_main_report_and_push_show_verification(monkeypatch, tmp_path, capsys):
    from tests.test_alarm_and_logs import _patched_main, _step1
    main, pushes = _patched_main(monkeypatch, tmp_path, _step1())

    def failing(ctx):
        alarm.add_not_written("метрики: немає рядка за сьогодні")
        return SimpleNamespace(ok=False, failed=["x"])

    monkeypatch.setattr(main, "verify_writes", failing)
    assert main.main([]) == 2
    out = capsys.readouterr().out
    first = "🚨🚨🚨 !!! НЕ ЗАПИСАНО: МЕТРИКИ: НЕМАЄ РЯДКА ЗА СЬОГОДНІ !!!"
    assert out.startswith(first) and "ВСЕ ЗАПИСАНО" not in out and pushes[0][0].startswith(first)

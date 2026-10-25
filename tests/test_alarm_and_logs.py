"""Тривога, локальні логи, мітка запуску, повторні запуски, розклад Europe/Warsaw (26.10.2026)."""
from __future__ import annotations

import csv
import importlib
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pipeline.step1_vacancies as step1
from models import REJECT_LOG_HEADER, SourceStatus
from pipeline import alarm, local_log, reject_log, schedule_gate
from scrapers.base import ScraperError

TODAY = date(2026, 10, 26)


@pytest.fixture(autouse=True)
def _clean_alarms():
    alarm.reset()
    yield
    alarm.reset()


def _decision(source="djinni.co", i=0, result="показано"):
    return {"source": source, "url": f"https://x/{i}", "label": f"A {i}", "result": result, "code": "—", "detail": "",
            "posted_date": "", "date_source": "", "category": "—", "verdict_haiku": "", "verdict_sonnet": ""}


# --- формат тривоги ---------------------------------------------------------
def test_alarm_line_format_uppercase_with_siren_markup():
    assert alarm.format_line("джерело djinni.co недоступне: HTTP 403") == \
        "🚨🚨🚨 **!!! ДЖЕРЕЛО DJINNI.CO НЕДОСТУПНЕ: HTTP 403 !!!**"


def test_collect_from_results_covers_sources_drive_and_claude():
    st = {"djinni.co": SourceStatus("djinni.co", "недоступне", "HTTP 403"),
          "work.ua": SourceStatus("work.ua", "OK", anomaly="0 карток, хоча зазвичай ≈40")}
    alarm.collect_from_results(
        {"source_statuses": st, "unevaluated_count": 3, "dedup_log_note": "лог дублів не вдалось оновити: 500"},
        {"source_statuses": {}, "metrics_saved": False, "metrics_error": "e"}, {"error": "x"},
        {"saved": False, "error": "quota"}, {"saved": False, "error": "quota2"})
    text = alarm.format_block()
    for part in ("DJINNI.CO НЕДОСТУПНЕ", "WORK.UA: 0 КАРТОК", "ПОМИЛКА CLAUDE", "ЛОГ ПОКАЗАНИХ", "МЕТРИКУ ФРІЛАНСУ",
                 "КРОК 2", "МЕТРИКУ НЕ ЗАПИСАНО", "РЕЗУЛЬТАТ ПЕРЕВІРКИ НЕ ЗАПИСАНО"):
        assert part in text
    assert all(line.startswith("🚨🚨🚨 **!!! ") and line.endswith(" !!!**") for line in text.splitlines())


def test_no_alarms_for_healthy_run():
    st = {"djinni.co": SourceStatus("djinni.co", "OK", count=40)}
    alarm.collect_from_results({"source_statuses": st, "dedup_log_note": ""}, {"source_statuses": {}, "metrics_saved": True},
                               {}, {"saved": True}, {"saved": True})
    assert not alarm.active()


# --- 0 карток там, де зазвичай десятки ----------------------------------------
def _history(source, counts, status="OK"):
    for i, c in enumerate(counts):
        local_log.record_source_count(date(2026, 10, 10 + i), source, c, status)


def test_zero_cards_alarm_only_with_baseline_of_dozens():
    _history("djinni.co", [40, 38, 45, 41])
    assert "≈41" in local_log.zero_cards_anomaly("djinni.co", 0) or "≈40" in local_log.zero_cards_anomaly("djinni.co", 0)
    assert local_log.zero_cards_anomaly("djinni.co", 5) == ""             # не нуль
    assert local_log.zero_cards_anomaly("work.ua", 0) == ""                # немає бази
    _history("dou", [2, 1, 3, 0])
    assert local_log.zero_cards_anomaly("dou", 0) == ""                    # зазвичай одиниці — не тривога


def test_scrape_all_records_counts_and_flags_403_and_zero(monkeypatch):
    _history("djinni.co", [40, 40, 40])

    class Mod:
        def __init__(self, fn): self.scrape = fn

    def boom():
        raise ScraperError("https://robota.ua -> HTTP 403")

    mods = {"djinni.co": Mod(lambda: []), "robota.ua": Mod(boom), "work.ua": Mod(lambda: [object()] * 3)}
    monkeypatch.setattr(step1, "SCRAPER_MODULES", mods)
    jobs, statuses, _ = step1._scrape_all(TODAY)
    assert statuses["robota.ua"].status == "недоступне" and "403" in statuses["robota.ua"].note
    assert "0 карток" in statuses["djinni.co"].anomaly and statuses["work.ua"].count == 3
    alarm.collect_from_results({"source_statuses": statuses}, None, None, None, None)
    text = alarm.format_block()
    assert "ROBOTA.UA НЕДОСТУПНЕ" in text and "DJINNI.CO: 0 КАРТОК" in text
    # факти пишуться локально по ходу роботи
    rows = list(csv.DictReader(open(local_log.logs_dir() / "source_counts.csv", encoding="utf-8-sig")))
    assert {(r["source"], r["status"]) for r in rows[-3:]} == {("djinni.co", "OK"), ("robota.ua", "недоступне"), ("work.ua", "OK")}


# --- локальний лог → Drive → перевірка рядків ---------------------------------
class FakeDrive:
    def __init__(self, fail_create=0, drop_rows=0, existing=()):
        self.fail_create, self.drop_rows, self.existing = fail_create, drop_rows, set(existing)
        self.created, self.written = [], {}

    def find(self, title, folder):
        return {"id": "x"} if title in self.existing else None

    def create(self, title, folder):
        if self.fail_create:
            self.fail_create -= 1
            raise RuntimeError("drive down")
        self.created.append(title)
        return f"id{len(self.created)}"

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: A002
        self.written[spreadsheetId] = body["values"]
        return SimpleNamespace(execute=lambda: None)

    def read(self, sid, rng):
        return [[r[0]] for r in self.written[sid]][: len(self.written[sid]) - self.drop_rows]


def _write(fake, decisions=None):
    svc = SimpleNamespace(spreadsheets=lambda: SimpleNamespace(values=lambda: SimpleNamespace(update=fake.update)))
    with patch.object(reject_log.drive, "find_file_by_title", fake.find), \
         patch.object(reject_log.drive, "create_spreadsheet", fake.create), \
         patch.object(reject_log, "sheets_service", lambda: svc), \
         patch.object(reject_log.sheets, "read_values", fake.read):
        return reject_log.write_reject_log(decisions if decisions is not None else [_decision(i=0), _decision("work.ua", 1)], TODAY)


def test_local_file_written_first_by_source_then_uploaded_and_verified():
    fake = FakeDrive()
    res = _write(fake, [_decision("work.ua", 1), _decision("djinni.co", 0), _decision("work.ua", 2)])
    local = list(csv.reader(open(res["local_path"], encoding="utf-8-sig")))
    assert local[0] == REJECT_LOG_HEADER and len(local) == 4
    assert [r[2] for r in local[1:]] == ["djinni.co", "work.ua", "work.ua"]  # по джерелах
    assert res["verified"] and res["error"] == "" and res["run_index"] == 0
    assert fake.created == [f"Лог відсіву 2026-10-26 (тест гітхаб)"]
    assert res["local_path"].endswith("2026-10-26.csv")


def test_upload_retried_once_then_succeeds():
    fake = FakeDrive(fail_create=1)
    res = _write(fake)
    assert res["verified"] and len(fake.created) == 1  # перша спроба впала, друга — ок


def test_upload_fails_twice_keeps_local_copy_and_reports_error():
    fake = FakeDrive(fail_create=2)
    res = _write(fake)
    assert not res["verified"] and "drive down" in res["error"]
    assert len(list(csv.reader(open(res["local_path"], encoding="utf-8-sig")))) == 3


def test_row_count_mismatch_is_unverified_and_local_copy_stays():
    fake = FakeDrive(drop_rows=1)
    res = _write(fake)
    assert not res["verified"] and "кількість рядків не збіглась" in res["error"]
    assert len(list(csv.reader(open(res["local_path"], encoding="utf-8-sig")))) == 3


def test_repeat_run_same_day_gets_separate_file_and_title():
    first = _write(FakeDrive())
    fake2 = FakeDrive()
    second = _write(fake2)
    third = _write(FakeDrive(existing={"Лог відсіву 2026-10-26 (тест гітхаб) (повтор 1)"}))
    assert second["run_index"] == 1 and second["title"].endswith("(тест гітхаб) (повтор 1)")
    assert second["local_path"].endswith("2026-10-26 (повтор 1).csv") and first["local_path"] != second["local_path"]
    assert third["run_index"] == 2 and third["title"].endswith("(повтор 2)")  # файл у Drive теж враховано


# --- метрики/перевірка: повтор, мітка «ЗАПУСК ПОЧАТО» ---------------------------
def test_metrics_retry_once_and_local_fallback(monkeypatch):
    from pipeline import step3_metrics as s3
    calls = []

    def flaky(sid, row):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("503")
        return "updated"

    monkeypatch.setattr(s3, "_get_or_create_metrics_sheet", lambda: "sid")
    monkeypatch.setattr(s3.sheets, "ensure_header", lambda *a, **k: None)
    monkeypatch.setattr(s3.sheets, "upsert_row_by_first_cell", flaky)
    step1_res = {"vacancies": [], "total_found_before_filters": 0, "source_statuses": {}}
    out = s3.run_step3(step1_res, {}, utc_today=TODAY)
    assert out["saved"] and out["action"] == "updated" and len(calls) == 2  # повторний запуск дня виправляє рядок

    monkeypatch.setattr(s3.sheets, "upsert_row_by_first_cell", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    out = s3.run_step3(step1_res, {}, utc_today=TODAY)
    assert not out["saved"] and (local_log.logs_dir() / "sheet_rows.csv").exists()


def test_start_marker_row_is_updated_in_place_at_end(monkeypatch):
    from pipeline import step4_selfcheck as s4
    events = []
    monkeypatch.setattr(s4, "_get_or_create_selfcheck_sheet", lambda: "S")
    monkeypatch.setattr(s4.sheets, "ensure_header", lambda *a, **k: None)
    monkeypatch.setattr(s4.sheets, "append_row", lambda sid, row: events.append(("append", row)) or 7)
    monkeypatch.setattr(s4.sheets, "update_row", lambda sid, n, row: events.append(("update", n, row)))
    monkeypatch.setattr(s4.sheets, "read_last_data_rows", lambda *a, **k: [])
    marker = s4.mark_run_started("2026-10-26 11:00 UTC")
    assert marker == {"sheet_id": "S", "row": 7}
    assert events[0][0] == "append" and events[0][1] == ["2026-10-26 11:00 UTC — ЗАПУСК ПОЧАТО"]
    st = {"djinni.co": SourceStatus("djinni.co")}
    res = s4.run_step4({"vacancies": [], "total_found_before_filters": 0, "source_statuses": st}, {}, {"saved": True},
                       timestamp_utc="2026-10-26 11:00 UTC", start_marker=marker)
    assert res["saved"] and [e[0] for e in events] == ["append", "update"] and events[1][1] == 7
    assert events[1][2][0] == "2026-10-26 11:00 UTC"  # мітку замінено результатом


def test_start_marker_failure_raises_alarm(monkeypatch):
    from pipeline import step4_selfcheck as s4
    monkeypatch.setattr(s4, "_get_or_create_selfcheck_sheet", lambda: (_ for _ in ()).throw(RuntimeError("no drive")))
    assert s4.mark_run_started("t") == {}
    assert "ЗАПУСК ПОЧАТО" in alarm.format_block()


def test_streak_ignores_start_marker_rows(monkeypatch):
    from pipeline import step4_selfcheck as s4
    from models import SELFCHECK_HEADER
    idx = SELFCHECK_HEADER.index("Лише Low/нуль при 5/5")
    yes = [""] * len(SELFCHECK_HEADER)
    yes[idx] = "так"
    rows = [yes, yes, ["2026-10-26 11:00 UTC — ЗАПУСК ПОЧАТО"]]  # останній — мітка поточного запуску
    monkeypatch.setattr(s4.sheets, "read_last_data_rows", lambda *a, **k: rows)
    assert s4._low_match_streak("S", "так") is True  # 1 + 2 історичних = 3


# --- розклад за Europe/Warsaw -------------------------------------------------
def test_schedule_gate_uses_warsaw_time_not_utc():
    # 26.10.2026 Варшава = CET (UTC+1): 12:00 = 11:00 UTC; 01.07.2026 = CEST (UTC+2): 12:00 = 10:00 UTC
    assert not schedule_gate.is_due(datetime(2026, 10, 26, 10, 59, tzinfo=timezone.utc))[0]
    assert schedule_gate.is_due(datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc))[0]
    assert not schedule_gate.is_due(datetime(2026, 7, 1, 9, 59, tzinfo=timezone.utc))[0]
    assert schedule_gate.is_due(datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc))[0]


def test_schedule_gate_runs_once_per_warsaw_day():
    t = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    assert schedule_gate.is_due(t)[0]
    schedule_gate.mark_started(t)
    assert not schedule_gate.is_due(datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc))[0]
    assert schedule_gate.is_due(datetime(2026, 10, 27, 11, 0, tzinfo=timezone.utc))[0]
    # 23:30 UTC = 00:30 наступного дня у Варшаві — нова дата, але ще до 12:00
    assert not schedule_gate.is_due(datetime(2026, 10, 26, 23, 30, tzinfo=timezone.utc))[0]


# --- main: перше повідомлення, push, exit code -----------------------------------
def _patched_main(monkeypatch, tmp_path, step1_result, rl_verified=True):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()
    main = importlib.import_module("main")
    pushes = []
    monkeypatch.setattr(main, "mark_run_started", lambda ts: {})
    monkeypatch.setattr(main, "run_step1", lambda **k: step1_result)
    monkeypatch.setattr(main, "write_reject_log", lambda d, t, summary=None: {"verified": rl_verified, "error": "кількість рядків не збіглась",
                                                               "local_path": "logs/2026-10-26.csv"})
    monkeypatch.setattr(main, "run_step1_2", lambda **k: {"projects": [], "source_statuses": {}, "metrics_saved": True})
    monkeypatch.setattr(main, "run_step2", lambda: {"findings": [], "total_emails_found": 0})
    monkeypatch.setattr(main, "run_step3", lambda *a, **k: {"saved": True})
    monkeypatch.setattr(main, "run_step3_tracker", lambda r: {"status": "OK", "note": ""})
    monkeypatch.setattr(main, "run_step4", lambda *a, **k: {"result": None, "saved": True})
    monkeypatch.setattr(main, "run_step5", lambda *a, **k: {"active": False})
    monkeypatch.setattr(main, "verify_writes", lambda ctx: SimpleNamespace(ok=True, failed=[]))
    monkeypatch.setattr(main, "format_selfcheck_block", lambda r: "")
    monkeypatch.setattr(main, "append_cost_row", lambda r: None)
    monkeypatch.setattr(main, "build_row", lambda *a, **k: [])
    monkeypatch.setattr(main.notify, "send_push", lambda message, urgent=False: pushes.append((message, urgent)) or True)
    return main, pushes


def _step1(statuses=None):
    return {"vacancies": [], "near_misses": [], "total_found_before_filters": 5, "decisions": [],
            "source_statuses": statuses or {"djinni.co": SourceStatus("djinni.co", "OK", count=5)}, "collection_methods": {}}


def test_main_alarm_is_first_in_console_and_push_and_exit_code_nonzero(monkeypatch, tmp_path, capsys):
    st = {"djinni.co": SourceStatus("djinni.co", "недоступне", "HTTP 403")}
    main, pushes = _patched_main(monkeypatch, tmp_path, _step1(st))
    code = main.main([])
    out = capsys.readouterr().out
    first = "🚨🚨🚨 **!!! ДЖЕРЕЛО DJINNI.CO НЕДОСТУПНЕ: HTTP 403 !!!**"
    assert code == 2 and out.startswith(first)
    assert pushes[0][0].startswith(first) and pushes[0][1] is True
    assert (tmp_path / "reports").glob("report_*.md") and open(next((tmp_path / "reports").glob("report_*.md")), encoding="utf-8").read().startswith(first)


def test_main_reject_log_mismatch_raises_alarm(monkeypatch, tmp_path, capsys):
    main, pushes = _patched_main(monkeypatch, tmp_path, _step1(), rl_verified=False)
    code = main.main([])
    out = capsys.readouterr().out
    assert code == 2 and out.startswith("🚨🚨🚨 **!!! ЛОГ ВІДСІВУ НЕ ВИВАНТАЖЕНО")
    assert "ЛОКАЛЬНА КОПІЯ: LOGS/2026-10-26.CSV" in out.splitlines()[0]


def test_main_clean_run_exit_zero_and_plain_push(monkeypatch, tmp_path, capsys):
    main, pushes = _patched_main(monkeypatch, tmp_path, _step1())
    assert main.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("✅ ВСЕ ЗАПИСАНО І ПЕРЕВІРЕНО") and "🚨" not in out
    assert pushes[0] == (main.notify.PUSH_MESSAGE_TEXT + "\n✅ ВСЕ ЗАПИСАНО І ПЕРЕВІРЕНО", False)


def test_main_unexpected_crash_returns_1_and_pushes_alarm(monkeypatch, tmp_path, capsys):
    main, pushes = _patched_main(monkeypatch, tmp_path, _step1())
    monkeypatch.setattr(main, "_run", lambda: (_ for _ in ()).throw(ValueError("бум")))
    assert main.main([]) == 1
    assert "АВАРІЯ ПАЙПЛАЙНА: VALUEERROR: БУМ" in capsys.readouterr().out and pushes[0][1] is True


def test_main_scheduled_flag_skips_when_not_due(monkeypatch, tmp_path):
    main, pushes = _patched_main(monkeypatch, tmp_path, _step1())
    monkeypatch.setattr(main.schedule_gate, "is_due", lambda: (False, "ще рано"))
    ran = []
    monkeypatch.setattr(main, "_run", lambda: ran.append(1) or 0)
    assert main.main(["--scheduled"]) == 0 and ran == [] and pushes == []

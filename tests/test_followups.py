"""--simulate-fail, --scheduled --check-only, рядок «ПІДСУМОК» у споживачах лога, «повтор N» у verify_writes,
звірка заголовків аркуша метрик (26.10.2026)."""
from __future__ import annotations

import importlib
import pathlib
import re
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from config import METRICS_KEY_COLUMNS, METRICS_SHEET_TITLE
from models import METRICS_HEADER, REJECT_LOG_HEADER
from pipeline import alarm, reject_log, schedule_gate, verify_writes as vw

TODAY = date(2026, 10, 26)


@pytest.fixture(autouse=True)
def _clean_alarms():
    alarm.reset()
    yield
    alarm.reset()


def _main(monkeypatch):
    main = importlib.import_module("main")
    pushes = []
    monkeypatch.setattr(main.notify, "send_push", lambda message, urgent=False: pushes.append((message, urgent)) or True)

    def boom(*a, **k):
        raise AssertionError("не мало викликатись")

    for name in ("_run", "run_step1", "mark_run_started", "write_reject_log"):
        monkeypatch.setattr(main, name, boom)
    for name in ("find_file_by_title", "create_spreadsheet", "get_modified_time"):
        monkeypatch.setattr(main.verify_writes.__globals__["drive"], name, boom)
    monkeypatch.setattr(main.schedule_gate, "mark_started", boom)
    return main, pushes


# --- 2. --simulate-fail ----------------------------------------------------------------
@pytest.mark.parametrize("key,target", [("log", "ЛОГ ВІДСІВУ"), ("shown", "ЛОГ ПОКАЗАНИХ"), ("metrics", "МЕТРИКИ"),
                                        ("check", "РЕЗУЛЬТАТ ПЕРЕВІРКИ"), ("sources", "ДЖЕРЕЛА")])
def test_simulate_fail_runs_alarm_push_exit_2_without_touching_anything(monkeypatch, capsys, key, target):
    main, pushes = _main(monkeypatch)
    assert main.main([f"--simulate-fail={key}"]) == 2
    out = capsys.readouterr().out
    first = out.splitlines()[0]
    assert first.startswith(f"🚨🚨🚨 !!! НЕ ЗАПИСАНО: {target}: СИМУЛЯЦІЯ ЗБОЮ") and first.endswith(" !!!")
    assert pushes[0][0].startswith(first) and pushes[0][1] is True


def test_simulate_fail_only_manual_and_valid_key(monkeypatch, capsys):
    main, pushes = _main(monkeypatch)
    assert main.main(["--scheduled", "--simulate-fail=log"]) == main.USAGE_EXIT_CODE
    assert main.main(["--scheduled", "--check-only", "--simulate-fail=log"]) == main.USAGE_EXIT_CODE
    assert main.main(["--simulate-fail=nope"]) == main.USAGE_EXIT_CODE
    assert main.main(["--simulate-fail"]) == main.USAGE_EXIT_CODE
    assert pushes == [] and "лише з ручним запуском" in capsys.readouterr().err


def test_simulate_fail_in_verify_writes_skips_other_checks(monkeypatch):
    called = []
    monkeypatch.setattr(vw, "CHECKS", [(n, lambda c: called.append(n) or (True, ""), None) for n, _, _ in vw.CHECKS])
    res = vw.verify_writes(vw.VerifyContext(utc_today=TODAY, step1_result={}), simulate_fail="metrics")
    assert not res.ok and called == [] and "метрики" in res.failed[0]


# --- 3. --scheduled --check-only -------------------------------------------------------------
def test_describe_prints_warsaw_time_and_reason():
    due, text = schedule_gate.describe(datetime(2026, 10, 26, 10, 59, tzinfo=timezone.utc))
    assert not due and "11:59 Europe/Warsaw (UTC 10:59)" in text and "розклад: 12:00 Europe/Warsaw" in text
    assert "ЗАПУСК НЕ ПОТРІБЕН, бо" in text and "ще раніше за 12:00" in text
    due, text = schedule_gate.describe(datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc))
    assert due and "ЗАПУСК ПОТРІБЕН, бо" in text and "12:00 Europe/Warsaw" in text
    schedule_gate.mark_started(datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc))
    due, text = schedule_gate.describe(datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc))
    assert not due and "запланований запуск уже був" in text


def test_check_only_prints_decision_exits_0_and_does_not_run(monkeypatch, capsys):
    main, pushes = _main(monkeypatch)
    monkeypatch.setattr(main.schedule_gate, "describe", lambda: (True, "ЗАПУСК ПОТРІБЕН, бо тест"))
    assert main.main(["--scheduled", "--check-only"]) == 0  # _run, mark_started, run_step1 — «boom», не викликались
    assert capsys.readouterr().out.strip() == "ЗАПУСК ПОТРІБЕН, бо тест" and pushes == []
    assert main.main(["--check-only"]) == main.USAGE_EXIT_CODE  # без --scheduled


# --- 4. «ПІДСУМОК» у споживачах лога ---------------------------------------------------------
def _values():
    rows = [["2026-10-26", "тест гітхаб", "djinni.co", f"u{i}", "x", "відсіяно", "ЗП", "", "", "", "—", "", ""] for i in range(3)]
    return [REJECT_LOG_HEADER] + rows + [reject_log.summary_row(TODAY, "знайдено 3")]


def test_data_rows_skips_header_and_summary():
    v = _values()
    assert len(reject_log.data_rows(v)) == 3 and reject_log.is_summary_row(v[-1]) and not reject_log.is_summary_row(v[1])
    assert all(not reject_log.is_summary_row(r) for r in reject_log.data_rows(v))


def test_verify_counts_rows_without_summary(monkeypatch):
    v = _values()
    monkeypatch.setattr(vw.drive, "find_file_by_title", lambda t, f=None: {"id": "rl"})
    monkeypatch.setattr(vw.drive, "get_modified_time", lambda fid: "2026-10-26T10:00:00Z")
    monkeypatch.setattr(vw.sheets, "read_values", lambda fid, rng="A1:ZZ": v)
    ctx = vw.VerifyContext(utc_today=TODAY, step1_result={"total_found_before_filters": 3}, reject_log={"title": "t"})
    assert vw.check_reject_log(ctx)[0]
    ctx.step1_result["total_found_before_filters"] = 4  # 3 рядки даних (без «ПІДСУМОК») < 90% від 4
    assert not vw.check_reject_log(ctx)[0]


def test_local_csv_summary_excluded_by_helper(tmp_path):
    import csv
    from pipeline import local_log
    log = local_log.RunLog(TODAY, 0, REJECT_LOG_HEADER)
    log.append_rows(_values()[1:])
    rows = list(csv.reader(open(log.path, encoding="utf-8-sig")))
    assert len(rows) == 5 and len(reject_log.data_rows(rows)) == 3


def test_summary_text_counts_only_decisions():
    main = importlib.import_module("main")
    text = main._reject_log_summary({"total_found_before_filters": 2, "vacancies": [],
                                     "decisions": [{"result": "відсіяно"}, {"result": "відсіяно"}]})
    assert "рядків 2" in text  # лічильник за decisions у пам'яті, «ПІДСУМОК» у них немає


def test_only_known_modules_consume_reject_log():
    """Охоронний тест: лог відсіву читають лише main (запис), verify_writes (через data_rows/is_summary_row)
    і сам reject_log. Новий споживач має пропускати «ПІДСУМОК» (reject_log.data_rows) — тому тест впаде."""
    root = pathlib.Path(__file__).resolve().parent.parent
    users = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/")) or rel == "conftest.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"REJECT_LOG_HEADER|reject_log import|import reject_log|pipeline\.reject_log|REJECT_LOG_FOLDER_ID", text):
            users.add(rel)
    assert users == {"config.py", "models.py", "main.py", "pipeline/reject_log.py", "pipeline/verify_writes.py",
                     "pipeline/local_log.py"}  # local_log — лише згадка в докстрингу, лог не читає


# --- 5. «повтор N» у verify_writes ---------------------------------------------------------------
def _repeat_world(monkeypatch, doc):
    monkeypatch.setattr(vw.drive, "find_file_by_title", lambda t, f=None: {"id": "dd"})
    monkeypatch.setattr(vw.docs, "read_full_text", lambda fid: doc)


def _repeat_ctx(own_lines, shown, run_index):
    return vw.VerifyContext(
        utc_today=TODAY,
        step1_result={"vacancies": [object()] * shown, "shown_log_lines": own_lines},
        reject_log={"title": "t", "run_index": run_index})


def test_repeat_run_counts_at_least_and_gives_no_false_alarm(monkeypatch):
    earlier = "2026-10-26 | https://a | Перший запуск 1 — A\n2026-10-26 | https://b | Перший запуск 2 — B\n"
    mine = ["2026-10-26 | https://c | Повтор — C"]
    _repeat_world(monkeypatch, "2026-10-20 | https://old | O — O\n" + earlier + mine[0] + "\n")  # 3 рядки за сьогодні, показано 1
    assert vw.check_shown_log(_repeat_ctx(mine, 1, run_index=1)) == (True, "")
    # той самий вміст для першого запуску дня — розбіжність (3 ≠ 1) є справжньою
    assert not vw.check_shown_log(_repeat_ctx(mine, 1, run_index=0))[0]


def test_repeat_run_missing_own_lines_is_caught_despite_earlier_rows(monkeypatch):
    earlier = "2026-10-26 | https://a | Перший запуск 1 — A\n2026-10-26 | https://b | Перший запуск 2 — B\n"
    mine = ["2026-10-26 | https://c | Повтор — C"]
    _repeat_world(monkeypatch, earlier)  # 2 ≥ 1 «не менше» виконано, але власного рядка немає
    ok, why = vw.check_shown_log(_repeat_ctx(mine, 1, run_index=1))
    assert not ok and "не записано 1 з 1" in why


def test_repeat_run_zero_shown_ok(monkeypatch):
    _repeat_world(monkeypatch, "2026-10-26 | https://a | Перший запуск — A\n")
    assert vw.check_shown_log(_repeat_ctx([], 0, run_index=2))[0]


# --- 6. заголовки аркуша метрик на початку запуску ---------------------------------------------------
def test_metrics_headers_printed_and_missing_key_column_raises_alarm_immediately(monkeypatch, capsys):
    main = importlib.import_module("main")
    header = [c for c in METRICS_HEADER if c != "Модель крок 1 (картки)"]
    missing = [c for c in METRICS_KEY_COLUMNS if c not in header]
    monkeypatch.setattr(main, "check_metrics_headers", lambda: (header, missing))
    main._check_metrics_headers()
    out = capsys.readouterr().out
    assert f"Заголовки аркуша «{METRICS_SHEET_TITLE}» ({len(header)}):" in out and "  1. Дата запуску" in out
    assert "🚨🚨🚨 **!!! В АРКУШІ" in out and "МОДЕЛЬ КРОК 1 (КАРТКИ)" in out and alarm.active()


def test_metrics_headers_all_present_no_alarm(monkeypatch, capsys):
    main = importlib.import_module("main")
    monkeypatch.setattr(main, "check_metrics_headers", lambda: (list(METRICS_HEADER), []))
    main._check_metrics_headers()
    assert "усі є в аркуші" in capsys.readouterr().out and not alarm.active()


def test_metrics_headers_sheet_missing_or_unreadable(monkeypatch, capsys):
    main = importlib.import_module("main")
    monkeypatch.setattr(main, "check_metrics_headers", lambda: (None, []))
    main._check_metrics_headers()
    assert "ще не існує" in capsys.readouterr().out and not alarm.active()
    monkeypatch.setattr(main, "check_metrics_headers", lambda: (_ for _ in ()).throw(RuntimeError("403")))
    main._check_metrics_headers()
    assert "НЕ ВДАЛОСЬ ПРОЧИТАТИ ЗАГОЛОВКИ" in alarm.format_block()


def test_check_metrics_headers_reads_real_header_row(monkeypatch):
    monkeypatch.setattr(vw.drive, "find_file_by_title", lambda t, f=None: {"id": "met"})
    monkeypatch.setattr(vw.sheets, "read_values", lambda fid, rng: [["Дата запуску", " Статус djinni.co "]])
    header, missing = vw.check_metrics_headers()
    assert header == ["Дата запуску", "Статус djinni.co"] and "Вартість Claude, $ разом" in missing
    monkeypatch.setattr(vw.drive, "find_file_by_title", lambda t, f=None: None)
    assert vw.check_metrics_headers() == (None, [])

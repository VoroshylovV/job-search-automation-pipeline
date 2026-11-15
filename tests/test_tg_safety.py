"""tg_market: захист від тихих втрат і збоїв (15.11.2026) — без мережі й API.

Покриває: неповне покриття стрічки, порожню стрічку, повтор пропущених моделлю постів, довгі пости повним текстом,
вигадані ключі, ліміт вартості, обірваний запис каналу, підстановку компанії лише коли вона однозначна,
ланцюг щомісячного циклу (журнал, зупинка, продовження).
"""
import csv
import json
import sys

import pytest

from tg_market import runlog
from tg_market.parse import Post


def _rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class _Resp:
    def __init__(self, text):
        self.text = text


def _page(ids, day="2026-11-10"):
    return "".join(
        f'<div class="tgme_widget_message" data-post="ch/{i}"><a class="tgme_widget_message_date">'
        f'<time datetime="{day}T10:00:00+00:00"></time></a><div class="tgme_widget_message_text">пост {i}</div></div>'
        for i in ids)


def _feed(monkeypatch, pages):
    """pages: {before|None: [ids]} — відповіді t.me/s для ?before=."""
    import tg_market.collect as col

    def fake_fetch(session, url, params=None):
        return _Resp(_page(pages.get((params or {}).get("before"), [])))

    monkeypatch.setattr(col, "fetch", fake_fetch)
    monkeypatch.setattr(col, "PAUSE_SECONDS", 0)
    return col


# ----------------------------------------------------------------------------- покриття стрічки
def test_fetch_channel_reports_incomplete_coverage_when_page_limit_hit(monkeypatch):
    from datetime import datetime, timezone
    col = _feed(monkeypatch, {None: [90, 91, 92], 90: [87, 88, 89], 87: [84, 85, 86]})
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    posts, problem = col.fetch_channel(object(), "ch", since, min_id=50, max_pages=2)
    assert [p.post_id for p in posts] == [87, 88, 89, 90, 91, 92]
    assert problem and "неповне покриття" in problem and "--max-pages 6" in problem   # до поста 50 не дійшли


def test_fetch_channel_complete_when_reaches_previous_run_or_channel_start(monkeypatch):
    from datetime import datetime, timezone
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    col = _feed(monkeypatch, {None: [90, 91, 92], 90: [87, 88, 89]})
    posts, problem = col.fetch_channel(object(), "ch", since, min_id=88, max_pages=2)
    assert [p.post_id for p in posts] == [89, 90, 91, 92] and problem is None      # дійшли до обробленого id
    col = _feed(monkeypatch, {None: [2, 3], 2: [1], 1: []})
    posts, problem = col.fetch_channel(object(), "ch", since, min_id=0, max_pages=5)
    assert [p.post_id for p in posts] == [1, 2, 3] and problem is None             # початок каналу — не проблема


def test_fetch_channel_empty_first_page_is_an_error_not_zero_posts(monkeypatch):
    from datetime import datetime, timezone
    from scrapers.base import ScraperError
    col = _feed(monkeypatch, {None: []})
    with pytest.raises(ScraperError, match="порожня стрічка"):
        col.fetch_channel(object(), "ch", datetime(2026, 8, 1, tzinfo=timezone.utc), min_id=10, max_pages=3)


def _main(monkeypatch, tmp_path, fetch_result, call_json, argv=()):
    import tg_market.collect as col
    monkeypatch.setattr(col, "DATA", tmp_path)
    monkeypatch.setattr(col, "load_channels", lambda panel: [{"handle": "ch", "panel": "core", "max_pages": "1"}])
    monkeypatch.setattr(col, "get_session", lambda: object())
    monkeypatch.setattr(col, "fetch_channel", fetch_result)
    monkeypatch.setattr(col, "call_json", call_json)
    monkeypatch.setattr(sys, "argv", ["collect", "--since", "2026-08-01", *argv])
    return col


def test_incomplete_channel_is_not_written_and_state_does_not_move(tmp_path, monkeypatch):
    (tmp_path / "state.json").write_text(json.dumps({"ch": 50}), encoding="utf-8")
    posts = [Post("ch", 90, "2026-11-10T10:00:00+00:00", 1, "t")]
    col = _main(monkeypatch, tmp_path, lambda *a, **k: (posts, "неповне покриття: тест"),
                lambda *a, **k: pytest.fail("Claude не має викликатись для неповного каналу"))
    with pytest.raises(SystemExit) as exc:
        col.main()
    assert exc.value.code == col.EXIT_PARTIAL
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {"ch": 50}
    assert not (tmp_path / "posts.csv").exists() and not (tmp_path / "raw").exists()
    run = _rows(tmp_path / "runs.csv")[0]
    assert run["status"] == "partial" and "неповне покриття" in run["problem_channels"]


def test_unavailable_channel_makes_run_partial_with_reason(tmp_path, monkeypatch):
    from scrapers.base import ScraperError

    def down(*a, **k):
        raise ScraperError("порожня стрічка: t.me/s/ch не віддав жодного поста")

    col = _main(monkeypatch, tmp_path, down, lambda *a, **k: pytest.fail("Claude не має викликатись"))
    with pytest.raises(SystemExit) as exc:
        col.main()
    assert exc.value.code == col.EXIT_PARTIAL
    run = _rows(tmp_path / "runs.csv")[0]
    assert run["status"] == "partial" and "ch: недоступний" in run["problem_channels"]


# ----------------------------------------------------------------------------- класифікація
def test_classify_retries_posts_missing_from_model_answer_and_drops_invented_keys(monkeypatch):
    import tg_market.collect as col
    posts = [Post("c", i, "2026-09-01T00:00:00+00:00", 1, f"t{i}") for i in range(4)]
    calls = []

    def fake(prompt, max_tokens, model=None, **kw):
        keys = [f"c/{i}" for i in range(4) if f'"c/{i}"' in prompt]
        calls.append(keys)
        if len(calls) == 1:      # перша відповідь: модель пропустила c/2 і c/3 та вигадала ключ c/99
            return {"posts": [{"key": "c/0", "is_vacancy": False}, {"key": "c/1", "is_vacancy": False},
                              {"key": "c/99", "is_vacancy": True, "vacancies": [{"title": "Вигадка"}]}]}
        return {"posts": [{"key": k, "is_vacancy": False} for k in keys]}

    monkeypatch.setattr(col, "call_json", fake)
    res = col.classify(posts)
    assert set(res) == {"c/0", "c/1", "c/2", "c/3"}          # пропущені дібрано, вигаданого ключа немає
    assert calls[1] == ["c/2", "c/3"]


def test_long_post_goes_alone_with_full_text(monkeypatch):
    import tg_market.collect as col
    tail = "ОСТАННЯ ВАКАНСІЯ ДАЙДЖЕСТУ"
    long_text = "x" * 3000 + tail
    posts = [Post("c", 1, "2026-09-01T00:00:00+00:00", 1, "короткий"), Post("c", 2, "2026-09-01T00:00:00+00:00", 1, long_text)]
    prompts = []

    def fake(prompt, max_tokens, model=None, **kw):
        prompts.append(prompt)
        keys = [f"c/{i}" for i in (1, 2) if f'"c/{i}"' in prompt]
        return {"posts": [{"key": k, "is_vacancy": False} for k in keys]}

    monkeypatch.setattr(col, "call_json", fake)
    col.classify(posts)
    assert len(prompts) == 2
    long_prompt = next(p for p in prompts if '"c/2"' in p)
    assert tail in long_prompt and '"c/1"' not in long_prompt   # кінець дайджесту не обрізано; пост іде окремо


def test_budget_limit_stops_run_without_writing_channel(tmp_path, monkeypatch):
    from claude_orchestrator.client import ClaudeBudgetExceededError

    def over(*a, **k):
        assert k.get("budget_limit") == 1.5
        raise ClaudeBudgetExceededError("ліміт бюджету запуску вичерпано")

    monkeypatch.setenv("TG_MARKET_BUDGET_USD", "1.5")
    posts = [Post("ch", 1, "2026-10-01T10:00:00+00:00", 1, "t")]
    col = _main(monkeypatch, tmp_path, lambda *a, **k: (posts, None), over)
    with pytest.raises(SystemExit) as exc:
        col.main()
    assert exc.value.code == col.EXIT_BUDGET
    assert not (tmp_path / "posts.csv").exists() and not (tmp_path / "state.json").exists()
    run = _rows(tmp_path / "runs.csv")[0]
    assert run["status"] == "budget" and run["budget_limit_usd"] == "1.5"
    assert (tmp_path / "raw" / "ch.jsonl").exists()               # текст збережено, канал добереться наступним запуском


def test_budget_env_zero_disables_limit_and_garbage_is_rejected(monkeypatch):
    import tg_market.collect as col
    monkeypatch.setenv("TG_MARKET_BUDGET_USD", "0")
    assert col.budget_limit() is None
    monkeypatch.setenv("TG_MARKET_BUDGET_USD", "шість")
    with pytest.raises(SystemExit):
        col.budget_limit()


# ----------------------------------------------------------------------------- обірваний запис
def test_recover_pending_removes_half_written_channel(tmp_path):
    runlog.append_csv(tmp_path / "posts.csv", runlog.POST_FIELDS, [
        {"run_id": "R1", "channel": "a", "post_id": 1}, {"run_id": "R2", "channel": "a", "post_id": 2},
        {"run_id": "R2", "channel": "b", "post_id": 7}])
    runlog.append_csv(tmp_path / "vacancies.csv", runlog.VAC_FIELDS, [
        {"run_id": "R2", "channel": "a", "post_id": 2, "title": "QA"}, {"run_id": "R1", "channel": "a", "post_id": 1, "title": "Dev"}])
    assert runlog.recover_pending(tmp_path) is None               # позначки немає — нічого не чіпаємо
    runlog.begin_commit(tmp_path, "R2", "a")
    info = runlog.recover_pending(tmp_path)
    assert info == {"run_id": "R2", "channel": "a", "posts": 1, "vacancies": 1}
    assert [(r["run_id"], r["channel"]) for r in _rows(tmp_path / "posts.csv")] == [("R1", "a"), ("R2", "b")]
    assert [r["title"] for r in _rows(tmp_path / "vacancies.csv")] == ["Dev"]
    assert (tmp_path / "posts.csv.bak").exists() and not (tmp_path / runlog.PENDING_FILE).exists()


def test_crash_between_files_is_cleaned_on_next_run(tmp_path, monkeypatch):
    posts = [Post("ch", 1, "2026-10-01T10:00:00+00:00", 1, "Junior QA")]

    def ok(prompt, max_tokens, model=None, **kw):
        return {"posts": [{"key": "ch/1", "is_vacancy": True, "vacancies": [{"title": "Junior QA", "is_it": True, "direction": "qa"}]}]}

    col = _main(monkeypatch, tmp_path, lambda *a, **k: (posts, None), ok)
    real_append = col.append_csv

    def crash_on_posts(path, fields, rows):
        if path.name == "posts.csv":
            raise OSError("диск відвалився")
        real_append(path, fields, rows)

    monkeypatch.setattr(col, "append_csv", crash_on_posts)
    with pytest.raises(OSError):
        col.main()
    assert len(_rows(tmp_path / "vacancies.csv")) == 1 and (tmp_path / runlog.PENDING_FILE).exists()

    monkeypatch.setattr(col, "append_csv", real_append)          # другий запуск: без збою
    col.main()
    assert len(_rows(tmp_path / "vacancies.csv")) == 1           # не 2: недописане прибрано, канал зібрано заново
    assert len(_rows(tmp_path / "posts.csv")) == 1
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {"ch": 1}
    assert not (tmp_path / runlog.PENDING_FILE).exists()


def test_runs_csv_migrates_to_new_schema_with_backup(tmp_path):
    old = [f for f in runlog.RUN_FIELDS if f not in ("unclassified_posts", "problem_channels", "budget_limit_usd")]
    p = tmp_path / "runs.csv"
    p.write_text(",".join(old) + "\n" + ",".join("x" for _ in old) + "\n", encoding="utf-8")
    row = runlog.build_run_row(run_id="R", started_at="s", since="2026-08-01", panel="core", channels=["a"], model="m",
                               prompt_text="p", posts=10, classified=9, vacancy_posts=1, vacancies=1, tokens={},
                               cost_usd=0.1, status="partial", git=("abc", False), problems=["a: недоступний"], budget_limit=6.0)
    runlog.append_csv(p, runlog.RUN_FIELDS, [row])
    rows = _rows(p)
    assert len(rows) == 2 and rows[0]["problem_channels"] == "" and rows[1]["problem_channels"] == "a: недоступний"
    assert rows[1]["unclassified_posts"] == "1" and (tmp_path / "runs.csv.bak").exists()


def test_posts_written_before_classified_column_count_as_classified_after_migration(tmp_path):
    """Шапка posts.csv до появи run_id/classified: після міграції classified порожнє — це не «нерозібрано»."""
    from tg_market.analyze import channel_metrics
    p = tmp_path / "posts.csv"
    p.write_text("channel,post_id,datetime,views,is_vacancy,vacancy_count,collected_at\n"
                 "a,1,2026-09-01T10:00:00+00:00,5,True,1,x\na,2,2026-09-02T10:00:00+00:00,7,False,0,x\n", encoding="utf-8")
    runlog.append_csv(p, runlog.POST_FIELDS, [{"run_id": "R", "channel": "a", "post_id": 3,
                                               "datetime": "2026-11-01T10:00:00+00:00", "views": 1,
                                               "classified": False, "is_vacancy": False, "vacancy_count": 0}])
    rows = _rows(p)
    assert rows[0]["classified"] == "" and rows[0]["run_id"] == "" and (tmp_path / "posts.csv.bak").exists()
    m = channel_metrics(rows, [], [])[0]
    assert m["posts"] == 3 and m["unclassified_posts"] == 1 and m["vacancy_post_share_pct"] == 50.0


# ----------------------------------------------------------------------------- компанія
def _m(channel, day, title, company, **kw):
    base = {"channel": channel, "datetime": f"2026-10-{day:02d}T10:00:00+00:00", "title": title, "company": company,
            "is_it": "True", "direction": "analytics", "level": "junior", "skills": "", "work_format": "remote",
            "salary_stated": "False", "link": ""}
    return {**base, **kw}


def test_company_is_imputed_only_when_unambiguous():
    from tg_market.analyze import dedup
    rows = [_m("a", 1, "Data Analyst", "Acme"), _m("b", 2, "Data Analyst", "Globex"),
            _m("c", 3, "Data Analyst", ""),                       # два роботодавці поруч — підставляти нема кого
            _m("a", 5, "Embedded Engineer", "Skif"), _m("b", 6, "Embedded Engineer", "")]   # один роботодавець — репост
    u = dedup(rows)
    da = sorted((x["company"], x["company_source"]) for x in u if x["title"] == "Data Analyst")
    assert da == [("", "unknown"), ("acme", "stated"), ("globex", "stated")]
    emb = [x for x in u if x["title"] == "Embedded Engineer"]
    assert len(emb) == 1 and emb[0]["mentions"] == 2 and emb[0]["company_source"] == "stated"


def test_company_not_imputed_from_outside_dedup_window():
    from tg_market.analyze import dedup
    old = {**_m("a", 1, "QA Engineer", "Acme"), "datetime": "2026-08-01T10:00:00+00:00"}
    u = dedup([old, _m("b", 20, "QA Engineer", "")])
    assert sorted(x["company_source"] for x in u) == ["stated", "unknown"]     # 80 днів — це вже не той самий набір


# ----------------------------------------------------------------------------- щомісячний цикл
def _cycle(monkeypatch, tmp_path, codes, extra_argv=()):
    """Запускає run_monthly з підміненими preflight і процесами кроків. codes: {крок: код завершення}."""
    import tg_market.run_monthly as rm
    data, out = tmp_path / "data", tmp_path / "data" / "out"
    out.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rm, "DATA", data)
    monkeypatch.setattr(rm, "OUT", out)
    monkeypatch.setattr(rm, "preflight", lambda args: ([], []))
    ran = []

    def runner(cmd, log_path):
        step = cmd[2].split(".")[-1]
        ran.append(step)
        code = codes.get(step, 0)
        if code == 0:
            trace = {"analyze": "unique_vacancies.csv", "compare": "compare_summary.csv", "monthly": "monthly_tg_vs_repo.csv"}.get(step)
            if trace:
                (out / trace).write_text("a,b\n1,2\n", encoding="utf-8")
        return code

    rc = rm.main(["--month", "2026-11", *extra_argv], runner=runner)
    journal = json.loads((data / "monthly_run.json").read_text(encoding="utf-8"))
    return rc, ran, journal


def test_cycle_runs_all_steps_in_order_and_skips_them_on_rerun(tmp_path, monkeypatch):
    rc, ran, j = _cycle(monkeypatch, tmp_path, {})
    assert rc == 0 and ran == ["collect", "analyze", "compare", "monthly", "drive_export"]
    assert all(j["steps"][s]["status"] == "ok" for s in ran)
    rc, ran, _ = _cycle(monkeypatch, tmp_path, {})
    assert rc == 0 and ran == []                                  # повторний запуск нічого не повторює


def test_cycle_stops_on_partial_collect_then_continues_when_accepted(tmp_path, monkeypatch):
    rc, ran, j = _cycle(monkeypatch, tmp_path, {"collect": 2})
    assert rc == 1 and ran == ["collect"]
    assert j["steps"]["collect"]["status"] == "partial" and j["steps"]["analyze"]["status"] == "not_run"
    rc, ran, j = _cycle(monkeypatch, tmp_path, {}, ["--accept-partial"])
    assert rc == 0 and ran == ["analyze", "compare", "monthly", "drive_export"]   # збір не повторюється
    assert j["steps"]["collect"]["status"] == "accepted"


def test_cycle_resumes_from_failed_step(tmp_path, monkeypatch):
    rc, ran, j = _cycle(monkeypatch, tmp_path, {"compare": 1})
    assert rc == 1 and ran == ["collect", "analyze", "compare"] and j["steps"]["monthly"]["status"] == "not_run"
    rc, ran, j = _cycle(monkeypatch, tmp_path, {})
    assert rc == 0 and ran == ["compare", "monthly", "drive_export"]


def test_cycle_marks_step_failed_when_it_leaves_no_fresh_result(tmp_path, monkeypatch):
    import tg_market.run_monthly as rm
    monkeypatch.setattr(rm, "DATA", tmp_path / "data")
    monkeypatch.setattr(rm, "OUT", tmp_path / "data" / "out")
    monkeypatch.setattr(rm, "preflight", lambda args: ([], []))
    rc = rm.main(["--month", "2026-11"], runner=lambda cmd, log: 0)      # кроки «успішні», але файлів не створили
    j = json.loads((tmp_path / "data" / "monthly_run.json").read_text(encoding="utf-8"))
    assert rc == 1 and j["steps"]["collect"]["status"] == "ok" and j["steps"]["analyze"]["status"] == "failed"
    assert "не оновив" in j["steps"]["analyze"]["note"]


def test_cycle_drive_export_without_folder_is_blocked_not_ok(tmp_path, monkeypatch):
    rc, ran, j = _cycle(monkeypatch, tmp_path, {"drive_export": 2})
    assert rc == 1 and j["steps"]["drive_export"]["status"] == "blocked"


def test_status_changes_nothing_and_new_cycle_archives_old_journal(tmp_path, monkeypatch):
    import tg_market.run_monthly as rm
    _cycle(monkeypatch, tmp_path, {})
    jpath = tmp_path / "data" / "monthly_run.json"
    before = jpath.read_text(encoding="utf-8")
    assert rm.main(["--month", "2026-12", "--status"]) == 1       # для грудня ще нічого не виконано
    assert jpath.read_text(encoding="utf-8") == before            # --status нічого не пише і не переносить
    rc, ran, j = _cycle(monkeypatch, tmp_path, {}, ())            # той самий цикл — журнал на місці
    assert ran == []
    monkeypatch.setattr(rm, "preflight", lambda args: ([], []))
    rm.main(["--month", "2026-12"], runner=lambda cmd, log: 1)    # новий цикл: старий журнал іде в архів
    assert json.loads(jpath.read_text(encoding="utf-8"))["cycle"] == "2026-12"
    archived = list((tmp_path / "data" / "monthly_logs").glob("journal_2026-11_*.json"))
    assert len(archived) == 1 and json.loads(archived[0].read_text(encoding="utf-8"))["cycle"] == "2026-11"


def _pre_args(**kw):
    import argparse
    base = dict(branch="main", allow_dirty=False, use_env_key=False, skip_ping=True)
    return argparse.Namespace(**{**base, **kw})


def test_preflight_refuses_wrong_branch_and_missing_or_env_key(tmp_path, monkeypatch):
    import tg_market.run_monthly as rm
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(rm, "REPO", tmp_path)
    monkeypatch.setattr(rm, "DATA", tmp_path / "data")
    monkeypatch.setattr(rm, "env_file_values", lambda: {"ANTHROPIC_API_KEY": "sk-pipeline", "TG_MARKET_DRIVE_FOLDER_ID": "F"})

    def fake_git(branch):
        return lambda *a: {"rev-parse": branch, "status": "", "fetch": "", "rev-list": "0"}[a[0]]

    monkeypatch.setattr(rm, "git", fake_git("tg-market-compare"))
    monkeypatch.setattr(rm, "_SHELL_KEY", "sk-telegram-9999")
    errors, _ = rm.preflight(_pre_args())
    assert len(errors) == 1 and "git checkout main" in errors[0]

    monkeypatch.setattr(rm, "git", fake_git("main"))
    assert rm.preflight(_pre_args()) == ([], [])                  # правильна гілка, окремий ключ у терміналі

    monkeypatch.setattr(rm, "_SHELL_KEY", "")
    errors, _ = rm.preflight(_pre_args())
    assert len(errors) == 1 and "не задано в терміналі" in errors[0]

    monkeypatch.setattr(rm, "_SHELL_KEY", "sk-pipeline")
    errors, _ = rm.preflight(_pre_args())
    assert len(errors) == 1 and "ключ щоденного пайплайна" in errors[0]


def test_preflight_stops_when_api_ping_fails_and_never_prints_full_key(tmp_path, monkeypatch, capsys):
    import tg_market.run_monthly as rm
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(rm, "REPO", tmp_path)
    monkeypatch.setattr(rm, "DATA", tmp_path / "data")
    monkeypatch.setattr(rm, "env_file_values", lambda: {"TG_MARKET_DRIVE_FOLDER_ID": "F"})
    monkeypatch.setattr(rm, "git", lambda *a: {"rev-parse": "main", "status": "", "fetch": "", "rev-list": "0"}[a[0]])
    monkeypatch.setattr(rm, "_SHELL_KEY", "sk-telegram-secret-1234")
    monkeypatch.setattr(rm, "api_ping", lambda model, key: "BadRequestError: credit balance is too low")
    errors, _ = rm.preflight(_pre_args(skip_ping=False))
    assert len(errors) == 1 and "credit balance" in errors[0]
    out = capsys.readouterr().out
    assert "…1234" in out and "sk-telegram-secret" not in out

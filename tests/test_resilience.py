"""Тести стійкості (10.10.2026): батчі оцінки, max_tokens, збій батча,
самоперевірка і ретраї Gmail. Без мережі й credentials."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from googleapiclient.errors import HttpError

import pipeline.step1_vacancies as step1
from claude_orchestrator import client
from claude_orchestrator.client import ClaudeCallError, ClaudeTruncatedError
from google_services import gmail
from models import RawJobPosting, SourceStatus
from pipeline.step4_selfcheck import _step1_status

TODAY = date(2026, 10, 10)


def _job(i):
    return RawJobPosting(source="djinni.co", title=f"Data Analyst {i}", company=f"Company {i}",
                         url=f"https://example.com/{i}", posted_raw="", salary_raw="", description_snippet="")


def _ev(i, **kw):
    return {"raw_index": i, "passes_criteria": True, "reject_reason": None,
            "normalized_title": f"Data Analyst {i}", "normalized_company": f"Company {i}",
            "match_level": "Medium", "match_reasoning": "ok", "veteran_bonus": False,
            "low_match_location": False, "low_match_location_reason": None,
            "posted_date": "2026-10-10", "date_undetermined": False, **kw}


def _all_ok(jobs, prompt_batch_size=None):
    return {"evaluations": [_ev(i) for i in range(len(jobs))]}


# ---- 1. батчі ---------------------------------------------------------------

def test_evaluate_uses_batches_of_configured_size():
    sizes = []

    def fake(prompt, **kw):
        n = prompt.count('"description_snippet": ')
        sizes.append(n)
        return {"evaluations": [_ev(i) for i in range(n)]}

    jobs = [_job(i) for i in range(45)]
    with patch.object(step1, "call_json", fake), patch.object(step1, "CLAUDE_EVAL_CHUNK_SIZE", 20):
        res = step1._evaluate(jobs, TODAY, "card")
    assert sizes == [20, 20, 5]
    assert sorted(res) == list(range(45))


def test_prompt_limits_text_fields_to_150_chars():
    from claude_orchestrator.prompts import VACANCY_EVAL_SYSTEM_PROMPT
    assert "150 символів" in VACANCY_EVAL_SYSTEM_PROMPT


def test_truncated_batch_is_split_in_half_and_retried():
    calls = []

    def fake(prompt, **kw):
        n = prompt.count('"description_snippet": ')
        calls.append(n)
        if n > 5:
            raise ClaudeTruncatedError("max_tokens")
        return {"evaluations": [_ev(i) for i in range(n)]}

    jobs = [_job(i) for i in range(12)]
    with patch.object(step1, "call_json", fake), patch.object(step1, "CLAUDE_EVAL_CHUNK_SIZE", 20):
        res = step1._evaluate(jobs, TODAY, "card")
    assert calls == [12, 6, 3, 3, 6, 3, 3]
    assert sorted(res) == list(range(12))
    assert not any(ev.get("eval_failed") for ev in res.values())
    # індекси відновлені правильно (кожна половина мапиться на свої вакансії)
    assert all(ev["raw_index"] == i % 3 for i, ev in res.items())


def test_client_raises_truncated_on_max_tokens_stop_reason(monkeypatch):
    msg = SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(type="text", text='{"evalua')])
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: msg))
    monkeypatch.setattr(client, "_client", lambda: fake_client)
    with pytest.raises(ClaudeTruncatedError):
        client.call_json("p")


def test_client_parses_normal_response(monkeypatch):
    msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"a": 1}')])
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: msg))
    monkeypatch.setattr(client, "_client", lambda: fake_client)
    assert client.call_json("p") == {"a": 1}


# ---- 2. збій батча не валить Крок 1 ----------------------------------------

def _scrape(jobs):
    return lambda today: (jobs, {"djinni.co": SourceStatus(source="djinni.co", status="OK")}, {"djinni.co": "перша_сторінка"})


def test_failed_batch_is_logged_and_rest_processed():
    jobs = [_job(i) for i in range(6)]
    state = {"n": 0}

    def fake(prompt, **kw):
        state["n"] += 1
        n = prompt.count('"description_snippet": ')
        if state["n"] == 1:  # перший батч (0-2) падає
            raise ClaudeCallError("невалідний JSON")
        return {"evaluations": [_ev(i) for i in range(n)]}

    with patch.object(step1, "_scrape_all", _scrape(jobs)), \
         patch.object(step1, "call_json", fake), \
         patch.object(step1, "CLAUDE_EVAL_CHUNK_SIZE", 3), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)

    by_url = {d["url"]: d for d in res["decisions"]}
    assert len(res["decisions"]) == 6
    assert [by_url[f"https://example.com/{i}"]["result"] for i in range(3)] == [step1.EVAL_FAILED_RESULT] * 3
    assert all(by_url[f"https://example.com/{i}"]["result"] == "показано" for i in range(3, 6))
    assert len(res["vacancies"]) == 3
    assert res["unevaluated_count"] == 3


def test_all_batches_failing_does_not_raise():
    jobs = [_job(i) for i in range(4)]

    def boom(prompt, **kw):
        raise RuntimeError("API down")

    with patch.object(step1, "_scrape_all", _scrape(jobs)), \
         patch.object(step1, "call_json", boom), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)
    assert res["vacancies"] == []
    assert res["unevaluated_count"] == 4
    assert {d["result"] for d in res["decisions"]} == {step1.EVAL_FAILED_RESULT}
    assert res["source_statuses"]["djinni.co"].status == "OK"  # збір не постраждав


# ---- 3. самоперевірка ------------------------------------------------------

def _ok_sources():
    names = ["djinni.co", "jobs.dou.ua", "robota.ua", "work.ua", "happymonday.ua"]
    return {s: SourceStatus(source=s, status="OK") for s in names}


def test_selfcheck_counts_sources_by_collection_and_reports_eval_failure_separately():
    status, note = _step1_status({"source_statuses": _ok_sources(), "dedup_level_used": 1, "unevaluated_count": 7})
    assert status == "ЧАСТКОВО"
    assert "оцінку не виконано для 7" in note
    assert "джерел перевірено" not in note  # збір 5/5 — це не проблема


def test_selfcheck_ok_when_no_eval_failures():
    status, note = _step1_status({"source_statuses": _ok_sources(), "dedup_level_used": 1, "unevaluated_count": 0})
    assert (status, note) == ("OK", "")


def test_selfcheck_trend_not_comparable_with_eval_failures(monkeypatch):
    from pipeline import step4_selfcheck as s4
    monkeypatch.setattr(s4, "_get_or_create_selfcheck_sheet", lambda: "sheet")
    monkeypatch.setattr(s4.sheets, "ensure_header", lambda *a, **k: None)
    monkeypatch.setattr(s4.sheets, "append_row", lambda *a, **k: None)
    monkeypatch.setattr(s4.sheets, "read_last_data_rows", lambda *a, **k: [])
    r = s4.run_step4({"source_statuses": _ok_sources(), "dedup_level_used": 1, "vacancies": [],
                      "total_found_before_filters": 10, "unevaluated_count": 3, "collection_methods": {}},
                     {}, {"saved": True}, "2026-10-10 12:00 UTC")["result"]
    assert r.sources_ok_count == 5
    assert not r.trend_comparable and "оцінку не виконано" in r.trend_comparable_reason
    assert r.only_low_or_zero_at_full_coverage == "н/д"


# ---- 4. Gmail ретраї -------------------------------------------------------

def _http_error(status, reason):
    body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    return HttpError(SimpleNamespace(status=status, reason=reason), body)


class _Req:
    def __init__(self, errors, result="ok"):
        self.errors, self.result, self.calls = list(errors), result, 0

    def execute(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


def test_gmail_single_60s_pause_then_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(gmail.time, "sleep", sleeps.append)
    req = _Req([_http_error(403, "rateLimitExceeded")])
    assert gmail._execute(req) == "ok"
    assert sleeps == [60]
    assert req.calls == 2


def test_gmail_429_also_single_pause(monkeypatch):
    sleeps = []
    monkeypatch.setattr(gmail.time, "sleep", sleeps.append)
    req = _Req([_http_error(429, "tooManyRequests")])
    assert gmail._execute(req) == "ok" and sleeps == [60]


def test_gmail_second_rate_limit_error_is_raised(monkeypatch):
    sleeps = []
    monkeypatch.setattr(gmail.time, "sleep", sleeps.append)
    req = _Req([_http_error(429, "x")] * 5)
    with pytest.raises(HttpError):
        gmail._execute(req)
    assert req.calls == 2 and sleeps == [60]


def test_gmail_does_not_retry_other_403(monkeypatch):
    sleeps = []
    monkeypatch.setattr(gmail.time, "sleep", sleeps.append)
    req = _Req([_http_error(403, "insufficientPermissions")])
    with pytest.raises(HttpError):
        gmail._execute(req)
    assert req.calls == 1 and sleeps == []


def test_step2_pauses_between_threads(monkeypatch):
    from pipeline import step2_mail
    sleeps = []
    monkeypatch.setattr(step2_mail.time, "sleep", sleeps.append)
    monkeypatch.setattr(step2_mail.gmail, "get_or_create_label", lambda n: "L")
    monkeypatch.setattr(step2_mail.gmail, "search_threads", lambda c, label_id_to_exclude: [{"id": "a"}, {"id": "b"}, {"id": "c"}])
    monkeypatch.setattr(step2_mail.gmail, "get_thread", lambda tid: {"messages": []})
    step2_mail.run_step2(company_names=[])
    assert len(sleeps) == 2


# ---- 5. правила оцінки (розбір логу 09.10) ---------------------------------

def test_prompt_rules_from_09_10_log_review():
    from claude_orchestrator.prompts import VACANCY_EVAL_SYSTEM_PROMPT, VACANCY_STAGE_CARD, VACANCY_STAGE_FULL
    assert "Power BI при наявному Tableau" in VACANCY_EVAL_SYSTEM_PROMPT and "НЕ є причиною відсіву" in VACANCY_EVAL_SYSTEM_PROMPT
    assert "Бізнес-аналітик (BA)" in VACANCY_STAGE_FULL and "Match-рівень Low" in VACANCY_STAGE_FULL
    assert "Удаленная работа" in VACANCY_STAGE_FULL and "Віддалена робота" in VACANCY_STAGE_FULL
    assert "BA/операційну аналітику НЕ відсівай" in VACANCY_STAGE_CARD


# ---- 6. Крок 2: вікно, ліміт, пропуск тредів, лічильники ---------------------

def test_gmail_query_window_is_two_days():
    assert "newer_than:2d" in gmail._build_query(["Acme"])


def test_search_threads_caps_and_logs(monkeypatch, caplog):
    pages = [
        {"threads": [{"id": str(i)} for i in range(30)], "nextPageToken": "p2"},
        {"threads": [{"id": str(i)} for i in range(30, 60)], "nextPageToken": "p3"},
    ]
    seen_max = []

    class _List:
        def __init__(self, resp):
            self.resp = resp

        def execute(self):
            return self.resp

    class _Threads:
        def list(self, **kw):
            seen_max.append(kw["maxResults"])
            return _List(pages.pop(0))

    service = SimpleNamespace(users=lambda: SimpleNamespace(threads=lambda: _Threads()))
    monkeypatch.setattr(gmail, "gmail_service", lambda: service)
    with caplog.at_level("INFO"):
        out = gmail.search_threads([], "label", max_threads=50)
    assert len(out) == 50
    assert seen_max == [50, 20]  # другий запит просить лише те, що лишилось до ліміту
    assert "запит повернув 50 тредів" in caplog.text and "ліміт 50" in caplog.text


def _stub_step2(monkeypatch, threads, get_thread):
    from pipeline import step2_mail
    monkeypatch.setattr(step2_mail.time, "sleep", lambda s: None)
    monkeypatch.setattr(step2_mail.gmail, "get_or_create_label", lambda n: "L")
    monkeypatch.setattr(step2_mail.gmail, "search_threads", lambda c, label_id_to_exclude: threads)
    monkeypatch.setattr(step2_mail.gmail, "get_thread", get_thread)
    return step2_mail


def test_step2_skips_unloadable_thread_and_reports_counts(monkeypatch, caplog):
    def get_thread(tid):
        if tid == "bad":
            raise RuntimeError("403 quota")
        return {"messages": []}

    step2 = _stub_step2(monkeypatch, [{"id": "a"}, {"id": "bad"}, {"id": "c"}], get_thread)
    with caplog.at_level("INFO"):
        res = step2.run_step2(company_names=[])
    assert (res["threads_found"], res["threads_loaded"], res["threads_skipped"]) == (3, 2, 1)
    assert "Тред bad не вдалося завантажити — пропущено" in caplog.text
    assert "тредів знайдено 3, завантажено 2, пропущено 1" in caplog.text


def test_selfcheck_step2_partial_when_threads_skipped():
    from pipeline.step4_selfcheck import _step2_status
    status, note = _step2_status({"company_names": [], "threads_found": 5, "threads_skipped": 2})
    assert status == "ЧАСТКОВО" and "2 з 5" in note
    assert _step2_status({"company_names": [], "threads_found": 5, "threads_skipped": 0})[0] == "OK"


# ---- 7. BA/Ops: Low лише при ≥1 збігу, інакше відсів за "роль" --------------

def test_prompt_ba_ops_requires_skill_match():
    from claude_orchestrator.prompts import VACANCY_STAGE_FULL
    assert "ОДИН збіг із навичками кандидата" in VACANCY_STAGE_FULL
    assert "Нуль збігів" in VACANCY_STAGE_FULL and 'reject_code "роль"' in VACANCY_STAGE_FULL
    assert "Бізнес-аналітик CRM Dynamics 365" in VACANCY_STAGE_FULL


def test_ba_with_zero_skill_matches_rejected_as_role_end_to_end():
    """Progresia «Бізнес-аналітик CRM Dynamics 365»: модель (за правилом
    промпту) повертає відсів «роль»; пайплайн має занести його в лог як такий."""
    job = RawJobPosting(source="work.ua", title="Бізнес-аналітик CRM Dynamics 365", company="Progresia",
                        url="https://work.ua/jobs/9000001/", posted_raw="", salary_raw="", description_snippet="")
    ev = {**_ev(0, passes_criteria=False), "reject_code": "роль", "reject_reason": "BA без збігів із навичками"}
    with patch.object(step1, "_scrape_all", _scrape([job])), \
         patch.object(step1, "call_json", lambda prompt, **kw: {"evaluations": [ev]}), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)
    assert res["vacancies"] == []
    assert res["decisions"][0]["code"] == "роль"

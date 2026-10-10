"""Тести двоетапної оцінки Кроку 1 (02.10.2026): детермінований стоп за
рівнем у назві, відсів без URL, вирішальний етап 2 по повному тексту,
витяг тексту зі сторінки, upsert метрики за датою. Мережа й Claude
замоковані."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

import pipeline.step1_vacancies as step1
from claude_orchestrator.prompts import build_vacancy_eval_prompt
from models import RawJobPosting, SourceStatus
from scrapers import detail

TODAY = date(2026, 10, 2)


def _job(i, title="Data Analyst", url=True, source="robota.ua"):
    return RawJobPosting(
        source=source, title=title, company=f"C{i}",
        url=f"https://robota.ua/company1/vacancy{1000 + i}" if url else None,
        posted_raw="", salary_raw="", description_snippet="Київ | короткий опис",
    )


def _ev(i, passes=True, code=None):
    return {
        "raw_index": i, "passes_criteria": passes, "reject_reason": "x" if not passes else None,
        "reject_code": code, "normalized_title": "Data Analyst", "normalized_company": f"C{i}",
        "match_level": "Medium", "match_reasoning": "SQL, Excel", "veteran_bonus": False,
        "low_match_location": False, "low_match_location_reason": None,
        "posted_date": TODAY.isoformat(), "date_undetermined": False,
    }


def _scrape(jobs):
    return lambda today: (jobs, {"robota.ua": SourceStatus("robota.ua")}, {"robota.ua": "перша_сторінка"})


import importlib  # noqa: E402

_REAL_STAGE2 = importlib.import_module("pipeline.step1_vacancies").__dict__.get("_confirm_with_full_text")


def _run(jobs, call_json, full_text=lambda job: None, real=True):
    patches = [
        patch.object(step1, "_scrape_all", _scrape(jobs)),
        patch.object(step1, "call_json", call_json),
        patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")),
        patch.object(step1, "fetch_full_text", full_text),
    ]
    for p in patches:
        p.start()
    try:
        return step1.run_step1(utc_today=TODAY, local_today=TODAY)
    finally:
        for p in patches:
            p.stop()


def test_seniority_title_is_rejected_before_claude():
    calls = []

    def cj(prompt, **kw):
        calls.append(prompt)
        return {"evaluations": []}

    jobs = [_job(0, title="Head of AnalyticsМ32до $5000"), _job(1, title="Senior Data Analyst")]
    res = _run(jobs, cj, real=False)
    assert calls == []
    assert {d["code"] for d in res["decisions"]} == {"досвід"}
    assert all("рівень у назві" in d["detail"] for d in res["decisions"])


def test_job_without_url_is_not_shown():
    res = _run([_job(0, url=False)], lambda p, **kw: {"evaluations": [_ev(0)]}, real=False)
    assert res["vacancies"] == []
    assert res["decisions"][0]["detail"] == "немає URL вакансії"


def test_stage2_full_text_overrides_card(monkeypatch):
    """Картка пройшла (Київ, без деталей), повний текст — лише офіс:
    вакансія НЕ показується, код «формат», дата зі «сторінки»."""
    monkeypatch.setattr(step1, "_confirm_with_full_text", _REAL_STAGE2)
    prompts = []

    def cj(prompt, **kw):
        prompt = kw.get("cache_prefix", "") + prompt  # статична частина йде окремо (prompt caching)
        prompts.append(prompt)
        if "ЕТАП 2 з 2" in prompt:
            return {"evaluations": [_ev(0, passes=False, code="формат")]}
        return {"evaluations": [_ev(0)]}

    res = _run([_job(0)], cj, full_text=lambda job: "Робота в офісі у Києві. " * 20, real=False)
    assert res["vacancies"] == []
    d = res["decisions"][0]
    assert d["code"] == "формат"
    assert d["date_source"] == "сторінка"
    assert len(prompts) == 2 and "full_text_available" in prompts[1]


def test_stage2_shown_marks_source_of_text(monkeypatch):
    monkeypatch.setattr(step1, "_confirm_with_full_text", _REAL_STAGE2)
    res = _run([_job(0), _job(1)], lambda p, **kw: {"evaluations": [_ev(0), _ev(1)]},
               full_text=lambda job: ("Повністю віддалено. " * 20) if job.url.endswith("1000") else None,
               real=False)
    details = sorted(d["detail"] for d in res["decisions"])
    assert details == ["Medium · лише картка", "Medium · сторінка"]


def test_prompt_stage_blocks():
    jobs = [_job(0)]
    card = build_vacancy_eval_prompt(jobs, today=TODAY)
    full = build_vacancy_eval_prompt(jobs, today=TODAY, stage="full", full_texts={0: "ПОВНИЙ ТЕКСТ"})
    assert "ЕТАП 1 з 2" in card and "ЕТАП 2 з 2" not in card
    assert "ЕТАП 2 з 2" in full and "ПОВНИЙ ТЕКСТ" in full and '"full_text_available": true' in full


def test_html_to_text_strips_noise():
    html = "<html><body><nav>меню</nav><script>x()</script><div id='job-description'>" + "Опис вакансії. " * 30 + "</div></body></html>"
    text = detail._html_to_text(html, "work.ua")
    assert "меню" not in text and "x()" not in text and text.startswith("Опис вакансії.")


def test_upsert_updates_same_date_row():
    from google_services import sheets
    updated = {}

    class _Svc:
        def spreadsheets(self): return self
        def values(self): return self
        def update(self, **kw):
            updated.update(kw)
            return self
        def execute(self): return {}

    with patch.object(sheets, "guard_not_forbidden", lambda _id: None), \
         patch.object(sheets, "read_values", lambda sid, rng: [["Дата"], ["2026-10-01"], ["2026-10-02"]]), \
         patch.object(sheets, "sheets_service", lambda: _Svc()), \
         patch.object(sheets, "append_row", lambda *a: pytest.fail("не мало бути append")):
        assert sheets.upsert_row_by_first_cell("sid", ["2026-10-02", 5, 1]) == "updated"
    assert updated["range"] == "A3:C3"


def test_happymonday_text_starts_at_vacancy_title():
    banner = "Work with Ukraine обʼєднує українських фахівців закордоном. Останнє оновлення. " * 5
    body = "Обов'язки: SQL-звіти, дашборди. Формат: віддалено. " * 30
    html = f"<html><body><div>{banner}</div><div class='v'><h1>Аналітик даних</h1><p>{body}</p></div></body></html>"
    text = detail._html_to_text(html, "happymonday.ua")
    assert text.startswith("Аналітик даних")
    assert "Work with Ukraine" not in text and "віддалено" in text

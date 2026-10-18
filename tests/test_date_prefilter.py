"""Правило Л п.4 (оновл. 18.10.2026): дата з картки поза вікном «сьогодні/вчора»
відсіюється ДО оцінки Claude; нерозпізнана дата — далі на оцінку."""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pipeline.step1_vacancies as step1
from models import RawJobPosting, SourceStatus

TODAY = date(2026, 10, 19)


def _job(i, posted_raw):
    return RawJobPosting(source="robota.ua", title=f"Data Analyst {i}", company=f"C{i}",
                         url=f"https://robota.ua/company1/vacancy{2000 + i}", posted_raw=posted_raw,
                         salary_raw="", description_snippet="опис")


def test_old_card_date_rejected_before_claude():
    jobs = [_job(0, "5 днів тому"), _job(1, "вчора"), _job(2, ""), _job(3, "2026-10-17T10:00:00")]
    sent = []

    def fake(prompt, **kw):
        payload = json.loads(prompt[prompt.index("Вакансії:\n") + len("Вакансії:\n"):])
        sent.extend(p["title"] for p in payload)
        return {"evaluations": [{"raw_index": k, "passes_criteria": False, "reject_code": "роль",
                                 "reject_reason": "x"} for k in range(len(payload))]}

    with patch.object(step1, "_scrape_all", lambda today: (jobs, {"robota.ua": SourceStatus("robota.ua")}, {})), \
         patch.object(step1, "call_json", fake), \
         patch.object(step1, "_read_dedup_log_with_retry", lambda: (None, "")):
        res = step1.run_step1(utc_today=TODAY, local_today=TODAY)

    assert sent == ["Data Analyst 1", "Data Analyst 2"]
    date_rejects = {d["label"].split(" — ")[0]: d for d in res["decisions"] if d["code"] == "дата"}
    assert set(date_rejects) == {"Data Analyst 0", "Data Analyst 3"}
    assert date_rejects["Data Analyst 3"]["posted_date"] == "2026-10-17"
    assert date_rejects["Data Analyst 0"]["date_source"] == "картка"

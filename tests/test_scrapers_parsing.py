"""Парсинг work.ua на реальній розмітці (фрагмент сторінки від 25.09.2026,
tests/fixtures/workua_listing.html) і розбір документа API robota.ua."""
from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from scrapers.robota import _api_doc_to_job
from scrapers.workua import JOB_LINK_RE, _find_card, _parse_card

FIXTURE = Path(__file__).parent / "fixtures" / "workua_listing.html"


def _workua_jobs():
    soup = BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "html.parser")
    jobs, seen = [], set()
    for link in soup.find_all("a", href=JOB_LINK_RE):
        job = _parse_card(_find_card(link))
        if job and job.url not in seen:
            seen.add(job.url)
            jobs.append(job)
    return jobs


def test_workua_card_extracts_company_date_salary_and_description():
    jobs = _workua_jobs()
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Керівник відділу аналітики"
    assert first.company == "Vuka money (PTY) ltd"
    assert first.posted_raw == "18 вересня 2026"
    assert first.salary_raw.startswith("67 000")
    assert "Дистанційно" in first.description_snippet
    assert first.url == "https://www.work.ua/jobs/8360277/"


def test_workua_second_card_company():
    assert _workua_jobs()[1].company == "Hay credito"


def test_robota_api_doc_to_job():
    doc = {
        "id": 11230349,
        "notebookId": 1020,
        "name": "Junior Data Analyst",
        "companyName": "ПУМБ",
        "date": "2026-09-25T09:00:00",
        "cityName": "Київ",
        "salary": 0,
        "shortDescription": "<b>SQL</b>, Python",
    }
    job = _api_doc_to_job(doc)
    assert job.url == "https://robota.ua/company1020/vacancy11230349"
    assert job.company == "ПУМБ" and job.salary_raw == ""
    assert job.description_snippet == "Київ | SQL , Python"


def test_robota_api_doc_without_id_is_skipped():
    assert _api_doc_to_job({"name": "X"}) is None

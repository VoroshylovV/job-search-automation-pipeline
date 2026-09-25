"""Тести для нових частин (25.09.2026): автооновлення таблиці відгуків
(pipeline/step3_tracker.plan_updates — чиста функція), пошук рядка
заголовків у таблиці з об'єднаною шапкою, парсинг відповіді API
Freelancehunt і вибір HTTP-сесії з імітацією браузера."""
from __future__ import annotations

from unittest.mock import patch

from google_services.sheets import find_header_row
from models import EmailFinding
from pipeline.step3_tracker import plan_updates
from scrapers import base
from scrapers.freelancehunt import _parse_api_item

HEADER = ["№з/п", "Компанія", "дата", "Посилання на вакансію", "Результат відгуку", "Примітки"]
TITLE_ROW = ["Interview — ПІП студента: Володимир Ворошилов"]


def _rows(*data_rows):
    return [TITLE_ROW, HEADER, *data_rows]


def _finding(sender, subject, status, date="Thu, 24 Sep 2026 10:00:00 +0000"):
    return EmailFinding(
        thread_id="t", message_id="m", sender=sender, subject=subject, date=date, summary="", status=status
    )


# --- find_header_row -------------------------------------------------------

def test_header_found_in_second_row_under_merged_title():
    assert find_header_row(_rows(), "Посилання на вакансію") == (1, 3)


def test_header_missing_returns_none():
    assert find_header_row(_rows(), "Немає такої") is None


# --- plan_updates ------------------------------------------------------------

def test_rejection_sets_result_and_appends_note():
    rows = _rows(["1", "Honeytech", "2026-09-05", "", "очікування", "Junior Data Analyst"])
    plan = plan_updates(rows, [_finding("HR <hr@honeytech.io>", "Your application", "відмова")])
    updates = dict(plan["updates"])
    assert updates["E3"] == "відмова"
    assert updates["F3"] == "Junior Data Analyst; Відмова 24.09.2026 (автопайплайн, лист: «Your application»)"
    assert plan["skipped"] == []


def test_invite_adds_note_only():
    rows = _rows(["1", "Ruby Labs", "2026-09-12", "", "очікування", ""])
    plan = plan_updates(rows, [_finding("Ruby Labs <jobs@rubylabs.com>", "Interview", "запрошення на співбесіду")])
    updates = dict(plan["updates"])
    assert "E3" not in updates
    assert updates["F3"].startswith("Запрошення на співбесіду 24.09.2026")


def test_idempotent_when_subject_already_in_notes():
    rows = _rows(["1", "Honeytech", "", "", "відмова", "Відмова 24.09.2026 (автопайплайн, лист: «Your application»)"])
    plan = plan_updates(rows, [_finding("hr@honeytech.io", "Your application", "відмова")])
    assert plan["updates"] == []


def test_ambiguous_company_with_two_pending_rows_is_skipped():
    rows = _rows(
        ["1", "Plamigo", "", "https://www.work.ua/jobs/1/", "очікування", ""],
        ["2", "Plamigo", "", "https://www.work.ua/jobs/2/", "очікування", ""],
    )
    plan = plan_updates(rows, [_finding("Plamigo <hr@plamigo.com>", "Результат", "відмова")])
    assert plan["updates"] == []
    assert "неоднозначно" in plan["skipped"][0]


def test_two_rows_but_only_one_pending_is_updated():
    rows = _rows(
        ["1", "Plamigo", "", "", "відмова", ""],
        ["2", "Plamigo", "", "", "очікування", ""],
    )
    plan = plan_updates(rows, [_finding("hr@plamigo.com", "Результат", "відмова")])
    assert dict(plan["updates"])["E4"] == "відмова"


def test_unrelated_email_and_non_actionable_status_ignored():
    rows = _rows(["1", "Honeytech", "", "", "очікування", ""])
    plan = plan_updates(
        rows,
        [
            _finding("noreply@djinni.co", "New jobs", "відмова"),
            _finding("hr@honeytech.io", "Thanks", "відгук на розгляді"),
        ],
    )
    assert plan["updates"] == [] and plan["skipped"] == []


def test_short_first_word_does_not_match_random_domain():
    rows = _rows(["1", "PwC Service Delivery Center", "", "", "очікування", ""])
    plan = plan_updates(rows, [_finding("hr@pwcomputers.com", "Re", "відмова")])
    assert plan["updates"] == []
    plan = plan_updates(rows, [_finding("careers@pwc.com", "Re", "відмова")])
    assert dict(plan["updates"])["E3"] == "відмова"


# --- Freelancehunt API --------------------------------------------------------

def test_parse_api_item_full():
    item = {
        "id": 123,
        "attributes": {
            "name": "Дашборд у Looker",
            "description": "Потрібен SQL",
            "budget": {"amount": 3000, "currency": "UAH"},
            "bid_count": 7,
            "published_at": "2026-09-25T10:00:00+03:00",
        },
        "links": {"self": {"web": "https://freelancehunt.com/project/dashboard/123.html"}},
    }
    p = _parse_api_item(item, "Базы данных и SQL")
    assert p.url.endswith("/123.html") and p.bids_count == 7 and p.budget_raw == "3000 UAH"
    assert p.posted_raw.startswith("2026-09-25")


def test_parse_api_item_without_budget_and_link_falls_back_to_id():
    p = _parse_api_item({"id": 9, "attributes": {"name": "X", "budget": None}}, "BI")
    assert p.url == "https://freelancehunt.com/project/9.html" and p.budget_raw == "" and p.bids_count is None


def test_parse_api_item_without_name_is_skipped():
    assert _parse_api_item({"id": 1, "attributes": {}}, "BI") is None


# --- HTTP-сесія ------------------------------------------------------------------

def test_impersonated_session_uses_curl_cffi_when_available():
    if base.cffi_requests is None:
        return  # curl_cffi не встановлено в цьому оточенні — фолбек перевірено нижче
    session = base.get_session(impersonate=True)
    assert type(session).__module__.startswith("curl_cffi")


def test_impersonated_session_falls_back_to_requests_without_curl_cffi():
    with patch.object(base, "cffi_requests", None):
        session = base.get_session(impersonate=True)
    assert type(session).__module__.startswith("requests")
    assert "Chrome" in session.headers["User-Agent"]


def test_freelancehunt_falls_back_to_html_when_api_fails():
    from scrapers import freelancehunt as fh

    def boom(_token):
        raise base.ScraperError("HTTP 401")
        yield  # pragma: no cover

    sentinel = object()
    with patch.object(fh, "FREELANCEHUNT_API_TOKEN", "bad"), \
         patch.object(fh, "_scrape_api", boom), \
         patch.object(fh, "_scrape_html", lambda: iter([sentinel])):
        assert list(fh.scrape()) == [sentinel]

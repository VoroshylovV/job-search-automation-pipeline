"""Пагінація і покриття джерел (Правило И п.8, 18.10.2026), без мережі."""
from datetime import date

import pytest

from models import RawJobPosting
from scrapers import djinni
from scrapers.base import Coverage, ScraperError, age_days, coverage_status, paginate

TODAY = date(2026, 10, 18)


def _j(i, posted=""):
    return RawJobPosting(source="x", title=f"t{i}", company="c", url=f"u{i}", posted_raw=posted,
                         salary_raw="", description_snippet="")


def test_age_days_formats():
    assert age_days("2026-10-16T09:00:00", TODAY) == 2
    assert age_days("22 вересня 2026", TODAY) == 26
    assert age_days("18 жовтня", TODAY) == 0
    assert age_days("вчора", TODAY) == 1
    assert age_days("3 дні тому", TODAY) == 3
    assert age_days("", TODAY) is None


def test_stops_when_older_than_window_and_marks_complete():
    pages = {1: [_j(1, "сьогодні"), _j(2, "вчора")], 2: [_j(3, "вчора"), _j(4, "3 дні тому")], 3: [_j(5, "сьогодні")]}
    jobs, cov = paginate(lambda n: pages.get(n, []), "q", max_pages=5, today=TODAY)
    assert [j.url for j in jobs] == ["u1", "u2", "u3", "u4"]
    assert cov == Coverage("q", 2, True, 3)


def test_stops_at_end_of_listing():
    pages = {1: [_j(1, "сьогодні")], 2: [_j(1, "сьогодні")]}  # друга сторінка нічого нового
    jobs, cov = paginate(lambda n: pages.get(n, []), "q", max_pages=5, today=TODAY)
    assert len(jobs) == 1 and cov.complete and cov.pages == 2


def test_max_pages_without_old_vacancies_is_incomplete():
    jobs, cov = paginate(lambda n: [_j(n * 10, "сьогодні")], "q", max_pages=3, today=TODAY)
    assert len(jobs) == 3 and not cov.complete and cov.pages == 3


def test_min_pages_respected_even_if_old_found_early():
    calls = []
    def page(n):
        calls.append(n)
        return [_j(n, "5 днів тому")]
    _, cov = paginate(page, "q", min_pages=3, max_pages=5, today=TODAY)
    assert calls == [1, 2, 3] and cov.complete


def test_error_on_later_page_keeps_jobs_incomplete_and_first_page_error_raises():
    def page(n):
        if n == 2:
            raise ScraperError("boom")
        return [_j(n, "сьогодні")]
    jobs, cov = paginate(page, "q", max_pages=5, today=TODAY)
    assert len(jobs) == 1 and not cov.complete
    with pytest.raises(ScraperError):
        paginate(lambda n: (_ for _ in ()).throw(ScraperError("x")), "q", today=TODAY)


def test_coverage_status_text():
    assert coverage_status([Coverage("a", 2, True, 3)]) == ("OK", "усі_сторінки")
    status, method = coverage_status([Coverage("a", 5, False, 1), Coverage("b", 1, True, 4)])
    assert status == "неповне покриття (5 сторінок, найстаріша переглянута — 1 днів)" and method == "оцінка"


def test_djinni_page_url_and_ai_crm_queries():
    assert djinni._page_url("https://djinni.co/jobs/keyword-data_analyst/?employment=remote", 2).endswith("&page=2")
    assert djinni._page_url("https://djinni.co/jobs/x/", 1) == "https://djinni.co/jobs/x/"
    for kw in ("product+analyst", "ai+analyst", "crm+analyst"):
        assert any(f"all_keywords={kw}" in u for u in djinni.LISTING_URLS)
    assert not any("all-keywords" in u or "keyword-product_analyst" in u for u in djinni.LISTING_URLS)


def test_happymonday_only_data_analyst_and_path_pagination():
    from scrapers import happymonday
    assert happymonday.LISTING_URLS == ["https://happymonday.ua/jobs-search/data-analyst"]
    assert happymonday._page_url(happymonday.LISTING_URLS[0], 2).endswith("/data-analyst/page/2")

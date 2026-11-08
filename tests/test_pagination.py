"""Пагінація і покриття джерел (Правило И п.8, 18.10.2026), без мережі."""
from datetime import date, datetime

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


def test_happymonday_card_date_not_used():
    """Останнє оновлення на картці ≠ дата публікації — posted_raw порожній."""
    from scrapers import happymonday
    html = ('<div><a href="/jobs/123">Junior Data Analyst</a>'
            '<span class="job-date">1 день тому</span></div>')
    jobs = happymonday._parse_page(html)
    assert len(jobs) == 1 and jobs[0].posted_raw == ""


# --- djinni: межа за «updated/оновлено» + дата публікації зі сторінки вакансії (08.11.2026) ---
import types

TODAY_D = date(2026, 11, 8)


@pytest.mark.parametrize("text,days", [
    ("updated 10h ago", 0), ("updated 15 min ago", 0), ("updated just now", 0),
    ("updated yesterday", 1), ("updated 3d ago", 3), ("updated 1w ago", 7),
    ("оновлено 5 год. тому", 0), ("оновлено 20 хв тому", 0), ("оновлено щойно", 0),
    ("оновлено вчора", 1), ("оновлено 3 дн. тому", 3), ("оновлено 2 тижні тому", 14),
    ("оновлено 1 міс. тому", 30), ("", None), ("updated recently", None),
])
def test_djinni_updated_formats(text, days):
    assert age_days(text, TODAY_D) == days


def test_djinni_published_date_formats():
    p = djinni.parse_published_date
    assert p('<span>Published 3 November</span>', TODAY_D) == date(2026, 11, 3)
    assert p('<span>Опубліковано 4 листопада</span>', TODAY_D) == date(2026, 11, 4)
    assert p('<span>Опубліковано 20 грудня</span>', TODAY_D) == date(2025, 12, 20)  # майбутнє -> минулий рік
    assert p('<span>Published 2 Jan 2026</span>', TODAY_D) == date(2026, 1, 2)
    assert p('{"datePosted": "2026-11-03T20:42:16.557856"} Published 1 May', TODAY_D) == date(2026, 11, 3)  # JSON-LD першим
    assert p('<p>нема дати</p>', TODAY_D) is None


def _card(i, updated, absolute=True):
    href = f"https://djinni.co/jobs/{i}-job-{i}/" if absolute else f"/jobs/{i}-job-{i}/"
    return (f'<div class="job-item" id="job-item-{i}"><a href="{href}"><header><h2 class="job-item__position">T{i}</h2>'
            f'<span class="small">Co</span></header></a><span>{updated}</span></div>')


def test_djinni_parse_page_absolute_and_relative_links():
    jobs = djinni._parse_page(_card(1, "updated 3d ago") + _card(2, "оновлено вчора", absolute=False))
    assert [j.url for j in jobs] == ["https://djinni.co/jobs/1-job-1/", "https://djinni.co/jobs/2-job-2/"]
    assert [j.posted_raw for j in jobs] == ["updated 3d ago", "оновлено вчора"]
    assert jobs[0].title == "T1" and jobs[0].company == "Co"


def _stub_fetch(monkeypatch, pages):
    calls = []

    def fake_fetch(session, url, **kw):
        calls.append(url)
        n = int(url.split("page=")[1]) if "page=" in url else 1
        return types.SimpleNamespace(text=pages.get(n, ""))
    monkeypatch.setattr(djinni, "fetch", fake_fetch)
    monkeypatch.setattr(djinni, "get_session", lambda *a, **k: object())
    monkeypatch.setattr(djinni, "LISTING_URLS", ["https://djinni.co/jobs/?x=1"])
    monkeypatch.setattr(djinni, "DJINNI_PAGE_SIZE", 3)
    monkeypatch.setattr(djinni, "datetime", types.SimpleNamespace(now=lambda: datetime(2026, 11, 8, 12, 0)))
    return calls


def test_djinni_tooltip_preferred_over_relative_text():
    card = ('<div class="job-item"><a href="https://djinni.co/jobs/1-a/"><header><h2 class="job-item__position">T</h2>'
            '<span class="small">Co</span></header></a>'
            '<span data-bs-original-title="09:45 05.11.2026">updated 3d ago</span></div>')
    j = djinni._parse_page(card)[0]
    assert j.posted_raw == "2026-11-05T09:45" and age_days(j.posted_raw, TODAY_D) == 3
    j2 = djinni._parse_page(_card(2, "оновлено 3 дн. тому"))[0]  # без tooltip -> відносний текст
    assert j2.posted_raw == "оновлено 3 дн. тому" and age_days(j2.posted_raw, TODAY_D) == 3


def test_djinni_non_monotonic_page_keeps_going_while_any_card_in_window(monkeypatch):
    pages = {1: _card(1, "updated 3d ago") + _card(2, "updated 11d ago") + _card(3, "updated 1d ago"),
             2: _card(4, "updated 5d ago") + _card(5, "updated 2h ago") + _card(6, "updated 9d ago"),
             3: _card(7, "updated 6d ago") + _card(8, "updated 7d ago") + _card(9, "updated 12d ago")}
    calls = _stub_fetch(monkeypatch, pages)
    jobs = list(djinni.scrape())
    # стор.1 має картку у вікні (1d) -> йдемо далі; стор.2 теж (2h); стор.3 без жодної -> зупинка
    assert djinni.LAST_COVERAGE[0] == Coverage("https://djinni.co/jobs/?x=1", 3, True, 12)
    assert len(jobs) == 9 and len(calls) == 3


def test_djinni_stops_on_first_page_without_card_in_window(monkeypatch):
    pages = {1: _card(1, "updated 3d ago") + _card(2, "updated 11d ago") + _card(3, "updated 4d ago"),
             2: _card(4, "updated 1d ago") + _card(5, "updated 1d ago") + _card(6, "updated 1d ago")}
    calls = _stub_fetch(monkeypatch, pages)
    list(djinni.scrape())
    assert djinni.LAST_COVERAGE[0] == Coverage("https://djinni.co/jobs/?x=1", 1, True, 11) and len(calls) == 1


def test_djinni_short_page_ends_listing(monkeypatch):
    calls = _stub_fetch(monkeypatch, {1: _card(1, "updated 1d ago") + _card(2, "updated 2h ago")})
    list(djinni.scrape())
    assert djinni.LAST_COVERAGE[0].complete and djinni.LAST_COVERAGE[0].pages == 1 and len(calls) == 1


def test_djinni_max_pages_is_incomplete_and_missing_date_warns(monkeypatch, caplog):
    monkeypatch.setattr(djinni, "MAX_LISTING_PAGES", 2)
    pages = {n: _card(n * 10, "updated 1d ago") + _card(n * 10 + 1, "updated 2h ago") + _card(n * 10 + 2, "без дати")
             for n in (1, 2, 3)}
    _stub_fetch(monkeypatch, pages)
    with caplog.at_level("WARNING"):
        jobs = list(djinni.scrape())
    assert not djinni.LAST_COVERAGE[0].complete and djinni.LAST_COVERAGE[0].pages == 2 and len(jobs) == 6
    assert "не знайдено дату оновлення на картці https://djinni.co/jobs/12-job-12/" in caplog.text


def test_detail_djinni_prepends_published_date_from_same_html(monkeypatch):
    from scrapers import detail
    html = ('<html><script type="application/ld+json">{"datePosted": "2026-11-03T20:42:16"}</script>'
            '<body><main>' + "текст вакансії " * 40 + '</main></body></html>')
    calls = []
    monkeypatch.setattr(detail, "fetch", lambda s, url, **k: calls.append(url) or types.SimpleNamespace(text=html))
    monkeypatch.setattr(detail, "get_session", lambda *a, **k: object())
    job = RawJobPosting(source="djinni.co", title="t", company="c", url="https://djinni.co/jobs/1-a/", posted_raw="",
                        salary_raw="", description_snippet="")
    text = detail.fetch_full_text(job)
    assert text.startswith("Дата публікації: 2026-11-03 | ") and len(calls) == 1

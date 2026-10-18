"""Скрапер happymonday.ua.

⚠️ Той самий застережний коментар щодо селекторів, що й у scrapers/djinni.py.
"""
from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from models import RawJobPosting
from config import DATE_WINDOW_DAYS, MAX_LISTING_PAGES
from scrapers.base import Coverage, ScraperError, fetch, get_session, paginate

COLLECTION_METHOD = "перша_сторінка"  # див. коментар у scrapers/djinni.py
LAST_COVERAGE: list[Coverage] = []

BASE_URL = "https://happymonday.ua"
LISTING_URLS = [
    # 18.10.2026: product-analyst / ai-analyst / crm-analyst — 404 (таких
    # сторінок на сайті немає), лишається тільки data-analyst.
    f"{BASE_URL}/jobs-search/data-analyst",
]


def _page_url(url: str, n: int) -> str:
    # Пагінація на сайті — шляхом /page/N (перевірено 18.10.2026), не ?page=N.
    return url if n == 1 else f"{url.rstrip('/')}/page/{n}"

# Посилання на вакансію: /jobs/<id>
JOB_LINK_RE = re.compile(r"^/jobs/\d+/?$")


def _parse_card(card) -> RawJobPosting | None:
    link = card.find("a", href=JOB_LINK_RE)
    if link is None:
        return None
    href = link["href"]
    url = BASE_URL + href if href.startswith("/") else href
    title = link.get_text(strip=True)

    company_el = card.find(class_=re.compile("company", re.I))
    company = company_el.get_text(strip=True) if company_el else ""

    date_el = card.find(class_=re.compile("date|time", re.I))
    posted_raw = date_el.get_text(strip=True) if date_el else ""

    full_text = card.get_text(" ", strip=True)

    return RawJobPosting(
        source="happymonday.ua",
        title=title,
        company=company,
        url=url,
        posted_raw=posted_raw,
        salary_raw="",
        description_snippet=full_text[:600],
    )


def _parse_page(html: str) -> list[RawJobPosting]:
    soup = BeautifulSoup(html, "html.parser")
    jobs, seen = [], set()
    for link in soup.find_all("a", href=JOB_LINK_RE):
        card = link.find_parent(["div", "li", "article"]) or link.parent
        job = _parse_card(card)
        if job is not None and job.url not in seen:
            seen.add(job.url)
            jobs.append(job)
    return jobs


def scrape() -> Iterator[RawJobPosting]:
    session = get_session()
    seen: set[str] = set()
    any_success = False
    last_error: Exception | None = None
    LAST_COVERAGE.clear()

    for listing_url in LISTING_URLS:
        try:
            jobs, cov = paginate(
                lambda n, u=listing_url: _parse_page(fetch(session, _page_url(u, n)).text),
                listing_url, max_pages=MAX_LISTING_PAGES, window_days=DATE_WINDOW_DAYS,
            )
        except ScraperError as exc:
            last_error = exc
            continue
        any_success = True
        LAST_COVERAGE.append(cov)
        for job in jobs:
            if job.url in seen:
                continue
            seen.add(job.url)
            yield job

    if not any_success:
        raise ScraperError(f"happymonday.ua: усі спроби провалились ({last_error})")


if __name__ == "__main__":
    for j in scrape():
        print(j)

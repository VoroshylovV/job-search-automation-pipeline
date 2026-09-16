"""Скрапер happymonday.ua.

⚠️ Той самий застережний коментар щодо селекторів, що й у scrapers/djinni.py.
"""
from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from models import RawJobPosting
from scrapers.base import ScraperError, fetch, get_session

BASE_URL = "https://happymonday.ua"
LISTING_URLS = [
    f"{BASE_URL}/jobs-search/data-analyst",
    f"{BASE_URL}/jobs-search/product-analyst",
]

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


def scrape() -> Iterator[RawJobPosting]:
    session = get_session()
    seen: set[str] = set()
    any_success = False
    last_error: Exception | None = None

    for listing_url in LISTING_URLS:
        try:
            resp = fetch(session, listing_url)
        except ScraperError as exc:
            last_error = exc
            continue
        any_success = True
        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.find_all("a", href=JOB_LINK_RE):
            card = link.find_parent(["div", "li", "article"]) or link.parent
            job = _parse_card(card)
            if job is None or job.url in seen:
                continue
            seen.add(job.url)
            yield job

    if not any_success:
        raise ScraperError(f"happymonday.ua: усі спроби провалились ({last_error})")


if __name__ == "__main__":
    for j in scrape():
        print(j)

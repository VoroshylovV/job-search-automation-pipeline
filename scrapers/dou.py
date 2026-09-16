"""Скрапер jobs.dou.ua.

⚠️ Той самий застережний коментар щодо селекторів, що й у scrapers/djinni.py —
див. його docstring. DOU теж server-rendered, тому requests+BeautifulSoup
має бути достатньо.
"""
from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from models import RawJobPosting
from scrapers.base import ScraperError, fetch, get_session

BASE_URL = "https://jobs.dou.ua"
LISTING_URL = f"{BASE_URL}/vacancies/"

# DOU дозволяє фільтр за категорією; "Analyst" — найближча вбудована категорія
# до Data/Product Analyst. Точний семантичний фільтр (роль/досвід) робить
# Claude на зібраних картках, це просто звужує вибірку на вході.
PARAMS = {"category": "Analyst"}

# Посилання на вакансію завжди виду /companies/<company-slug>/vacancies/<id>/
JOB_LINK_RE = re.compile(r"^/companies/[\w-]+/vacancies/\d+/?$")


def _parse_card(card) -> RawJobPosting | None:
    link = card.find("a", href=JOB_LINK_RE) or card.find(
        "a", class_=re.compile("vt|title", re.I)
    )
    if link is None or not link.get("href"):
        return None
    href = link["href"]
    url = href if href.startswith("http") else BASE_URL + href
    title = link.get_text(strip=True)

    company_el = card.find(class_=re.compile("company", re.I))
    company = company_el.get_text(strip=True) if company_el else ""

    date_el = card.find(class_=re.compile("date", re.I))
    posted_raw = date_el.get_text(strip=True) if date_el else ""

    full_text = card.get_text(" ", strip=True)

    return RawJobPosting(
        source="jobs.dou.ua",
        title=title,
        company=company,
        url=url,
        posted_raw=posted_raw,
        salary_raw="",
        description_snippet=full_text[:600],
    )


def scrape() -> Iterator[RawJobPosting]:
    session = get_session()
    try:
        resp = fetch(session, LISTING_URL, params=PARAMS)
    except ScraperError as exc:
        raise ScraperError(f"jobs.dou.ua: {exc}") from exc

    soup = BeautifulSoup(resp.text, "html.parser")
    seen: set[str] = set()
    # li.l-vacancy — типовий контейнер картки на DOU; якщо розмітка зміниться,
    # fallback йде через прямий пошук посилань за JOB_LINK_RE.
    cards = soup.select("li.l-vacancy") or [
        a.find_parent(["li", "div"]) or a for a in soup.find_all("a", href=JOB_LINK_RE)
    ]
    for card in cards:
        job = _parse_card(card)
        if job is None or job.url in seen:
            continue
        seen.add(job.url)
        yield job


if __name__ == "__main__":
    for j in scrape():
        print(j)

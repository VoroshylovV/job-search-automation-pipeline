"""Скрапер work.ua.

⚠️ Той самий застережний коментар щодо селекторів, що й у scrapers/djinni.py.
Work.ua історично більш "класичний" server-rendered сайт (перевірено раніше
в сесії розробки — сторінки віддавались одразу з повним HTML), тому SPA/JSON
fallback тут, на відміну від robota.py, не робимо.
"""
from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from models import RawJobPosting
from scrapers.base import ScraperError, fetch, get_session

COLLECTION_METHOD = "перша_сторінка"  # див. коментар у scrapers/djinni.py

BASE_URL = "https://www.work.ua"
LISTING_URLS = [
    f"{BASE_URL}/jobs-remote-data+analyst/",
    f"{BASE_URL}/jobs-remote-product+analyst/",
]

# Посилання на вакансію: /jobs/<id>/
JOB_LINK_RE = re.compile(r"^/jobs/\d+/?$")


# Дата з атрибута title посилання: "Аналітик, вакансія від 22 вересня 2026"
TITLE_DATE_RE = re.compile(r"вакансія від (.+)$")


def _parse_card(card) -> RawJobPosting | None:
    """Картка вакансії work.ua (розмітка звірена з живим HTML 25.09.2026):
    div.card.job-link > h2 > a[href=/jobs/<id>/, title="<назва>, вакансія від <дата>"];
    компанія — span.strong-600 поруч з іконкою .glyphicon-company;
    короткий опис — p.ellipsis; вимоги/локація — div.text-indent."""
    link = card.find("a", href=JOB_LINK_RE)
    if link is None:
        return None
    href = link["href"]
    url = BASE_URL + href if href.startswith("/") else href
    title = link.get_text(strip=True)

    posted_raw = ""
    m = TITLE_DATE_RE.search(link.get("title", ""))
    if m:
        posted_raw = m.group(1).strip()

    company = ""
    icon = card.find(class_="glyphicon-company")
    if icon is not None:
        name_el = icon.find_next("span", class_="strong-600")
        if name_el is not None:
            company = name_el.get_text(strip=True)

    salary_raw = ""
    salary_icon = card.find(class_="glyphicon-hryvnia-fill")
    if salary_icon is not None:
        sal_el = salary_icon.find_next("span", class_="strong-600")
        if sal_el is not None:
            salary_raw = re.sub(r"\s+", " ", sal_el.get_text(strip=True))  # нерозривні пробіли -> звичайні

    details = [el.get_text(" ", strip=True) for el in card.find_all(class_="text-indent")]
    desc_el = card.find("p", class_="ellipsis")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    snippet = re.sub(r"\s+", " ", " | ".join(x for x in [*details, description] if x))

    return RawJobPosting(
        source="work.ua",
        title=title,
        company=company,
        url=url,
        posted_raw=posted_raw,
        salary_raw=salary_raw,
        description_snippet=snippet[:600],
    )


def _find_card(link):
    """Уся картка вакансії (div.card.job-link), а не найближчий div навколо h2 —
    раніше бралась саме обгортка заголовка, тому компанія/опис губились."""
    return (
        link.find_parent(class_="job-link")
        or link.find_parent(class_="card")
        or link.find_parent(["div", "li"])
        or link.parent
    )


def scrape() -> Iterator[RawJobPosting]:
    session = get_session(impersonate=True)
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
            job = _parse_card(_find_card(link))
            if job is None or job.url in seen:
                continue
            seen.add(job.url)
            yield job

    if not any_success:
        raise ScraperError(f"work.ua: усі спроби провалились ({last_error})")


if __name__ == "__main__":
    for j in scrape():
        print(j)

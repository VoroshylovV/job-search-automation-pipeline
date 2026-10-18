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
from config import DATE_WINDOW_DAYS, MAX_LISTING_PAGES
from scrapers.base import Coverage, ScraperError, fetch, get_session, paginate

COLLECTION_METHOD = "перша_сторінка"  # див. коментар у scrapers/djinni.py
LAST_COVERAGE: list[Coverage] = []
# Наступні «сторінки» DOU підвантажує POST-запитом xhr-load (кнопка «Більше
# вакансій»): відповідь JSON {"html": ..., "last": bool}, крок — 20 вакансій.
XHR_URL = "https://jobs.dou.ua/vacancies/xhr-load/"
XHR_PAGE_SIZE = 20

BASE_URL = "https://jobs.dou.ua"
LISTING_URL = f"{BASE_URL}/vacancies/"

# DOU дозволяє фільтр за категорією; "Analyst" — найближча вбудована категорія
# до Data/Product Analyst. Точний семантичний фільтр (роль/досвід) робить
# Claude на зібраних картках, це просто звужує вибірку на вході.
# Фільтри на рівні запиту (додано 02.10.2026): remote + досвід "<1 року" і
# "1-3 роки" (діапазон 1-3 лишається, бо критерій пропускає нижню межу
# ≤1.5 року з капом Match на Medium; фіксовані 2+ роки відсіє повна сторінка).
# Параметр `remote` на DOU — прапорець без значення (?remote), тому значення "".
PARAMS_LIST = [
    {"category": "Analyst", "remote": "", "exp": "0-1"},
    {"category": "Analyst", "remote": "", "exp": "1-3"},
    # Правило И п.3 і п.8 (18.10.2026): окремі пошуки «data analyst», «AI»,
    # «CRM analyst» (remote) — частина вакансій на DOU не в категорії Analyst.
    {"search": "data analyst", "remote": ""},
    {"search": "AI", "remote": ""},
    {"search": "CRM analyst", "remote": ""},
]

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


def _cards_to_jobs(html: str) -> list[RawJobPosting]:
    soup = BeautifulSoup(html, "html.parser")
    # li.l-vacancy — типовий контейнер картки на DOU; якщо розмітка зміниться,
    # fallback йде через прямий пошук посилань за JOB_LINK_RE.
    cards = soup.select("li.l-vacancy") or [
        a.find_parent(["li", "div"]) or a for a in soup.find_all("a", href=JOB_LINK_RE)
    ]
    jobs, seen = [], set()
    for card in cards:
        job = _parse_card(card)
        if job is not None and job.url not in seen:
            seen.add(job.url)
            jobs.append(job)
    return jobs


def _fetch_page(session, params: dict, n: int, state: dict) -> list[RawJobPosting]:
    if n == 1:
        resp = fetch(session, LISTING_URL, params=params)
        state["referer"] = resp.url
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', resp.text)
        state["csrf"] = m.group(1) if m else (session.cookies.get("csrftoken") or "")
        return _cards_to_jobs(resp.text)
    if state.get("last"):
        return []
    try:
        resp = session.post(
            XHR_URL, params=params,
            data={"csrfmiddlewaretoken": state.get("csrf", ""), "count": XHR_PAGE_SIZE * (n - 1)},
            headers={"Referer": state.get("referer", LISTING_URL), "X-Requested-With": "XMLHttpRequest"},
            timeout=15,
        )
        payload = resp.json() if resp.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001
        raise ScraperError(f"jobs.dou.ua xhr-load: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScraperError(f"jobs.dou.ua xhr-load: HTTP {resp.status_code}")
    state["last"] = bool(payload.get("last"))
    return _cards_to_jobs(payload.get("html") or "")


def scrape() -> Iterator[RawJobPosting]:
    session = get_session()
    seen: set[str] = set()
    any_success = False
    last_error: Exception | None = None
    LAST_COVERAGE.clear()
    for params in PARAMS_LIST:
        state: dict = {}
        try:
            jobs, cov = paginate(lambda n, p=params, st=state: _fetch_page(session, p, n, st),
                                 str(params), max_pages=MAX_LISTING_PAGES, window_days=DATE_WINDOW_DAYS)
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
        raise ScraperError(f"jobs.dou.ua: усі запити провалились ({last_error})")


if __name__ == "__main__":
    for j in scrape():
        print(j)

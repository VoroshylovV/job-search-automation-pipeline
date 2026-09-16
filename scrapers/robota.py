"""Скрапер robota.ua.

⚠️ Той самий застережний коментар щодо селекторів, що й у scrapers/djinni.py.
Додатково: robota.ua — сучасний SPA (React/Next.js), тому є ризик, що
списки вакансій рендеряться на клієнті, а не в початковому HTML. Функція
`_extract_embedded_json` пробує витягти вбудований JSON-стан сторінки
(типовий патерн для Next.js: `<script id="__NEXT_DATA__">`), що працює
БЕЗ headless-браузера навіть для SPA. Якщо і це не спрацює на практиці —
дивись README, розділ "Якщо requests+BeautifulSoup не бачить вакансій":
там описано, як підключити playwright замість requests лише для цього
модуля, не чіпаючи решту пайплайну.
"""
from __future__ import annotations

import json
import re
from typing import Iterator

from bs4 import BeautifulSoup

from models import RawJobPosting
from scrapers.base import ScraperError, fetch, get_session

BASE_URL = "https://robota.ua"
LISTING_URLS = [
    f"{BASE_URL}/zapros/data-analyst/ukraine",
    f"{BASE_URL}/zapros/product-analyst/ukraine",
]

# Посилання на вакансію: /companyXXXXX/vacancyXXXXXXXX
JOB_LINK_RE = re.compile(r"/company\d+/vacancy\d+")


def _extract_embedded_json(soup: BeautifulSoup) -> list[dict]:
    """Пробує знайти вбудований JSON зі списком вакансій у <script> тегах."""
    results: list[dict] = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text or "vacanc" not in text.lower():
            continue
        # Шукаємо будь-який JSON-масив об'єктів з ключем на кшталт "vacancyId"/"title"
        for match in re.finditer(r"\{[^{}]{0,2000}?\"title\"[^{}]{0,2000}?\}", text):
            try:
                obj = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
            if "title" in obj:
                results.append(obj)
    return results


def _parse_html_cards(soup: BeautifulSoup) -> Iterator[RawJobPosting]:
    seen: set[str] = set()
    for link in soup.find_all("a", href=JOB_LINK_RE):
        href = link["href"]
        url = href if href.startswith("http") else BASE_URL + href
        if url in seen:
            continue
        seen.add(url)
        card = link.find_parent(["li", "div", "article"]) or link.parent
        title = link.get_text(strip=True) or (card.get_text(" ", strip=True)[:80])
        company_el = card.find(class_=re.compile("company", re.I)) if card else None
        company = company_el.get_text(strip=True) if company_el else ""
        full_text = card.get_text(" ", strip=True) if card else title
        yield RawJobPosting(
            source="robota.ua",
            title=title,
            company=company,
            url=url,
            posted_raw="",
            salary_raw="",
            description_snippet=full_text[:600],
        )


def scrape() -> Iterator[RawJobPosting]:
    session = get_session()
    any_success = False
    last_error: Exception | None = None
    seen_urls: set[str] = set()

    for listing_url in LISTING_URLS:
        try:
            resp = fetch(session, listing_url)
        except ScraperError as exc:
            last_error = exc
            continue
        any_success = True
        soup = BeautifulSoup(resp.text, "html.parser")

        found_any_on_page = False
        for job in _parse_html_cards(soup):
            if job.url in seen_urls:
                continue
            seen_urls.add(job.url)
            found_any_on_page = True
            yield job

        if not found_any_on_page:
            # HTML-парсинг нічого не дав — ймовірно, SPA рендерить на клієнті.
            # Пробуємо запасний варіант: вбудований JSON-стан сторінки.
            for obj in _extract_embedded_json(soup):
                url = obj.get("url") or obj.get("link") or ""
                if url and not url.startswith("http"):
                    url = BASE_URL + url
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                yield RawJobPosting(
                    source="robota.ua",
                    title=str(obj.get("title", "")),
                    company=str(obj.get("companyName", obj.get("company", ""))),
                    url=url or None,
                    posted_raw=str(obj.get("publicationDate", "")),
                    salary_raw=str(obj.get("salary", "")),
                    description_snippet=str(obj.get("description", ""))[:600],
                )

    if not any_success:
        raise ScraperError(f"robota.ua: усі спроби провалились ({last_error})")


if __name__ == "__main__":
    for j in scrape():
        print(j)

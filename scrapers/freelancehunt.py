"""Скрапер Freelancehunt.com для Кроку 1.2 (фріланс-проєкти).

⚠️ ТА САМА засторога про селектори, що й у scrapers/djinni.py: пісочниця
розробки не має прямого вихідного доступу в інтернет, тому розмітка нижче
побудована на основі спостережень під час ручних пілотних запусків
(22.09.2026) через окремий інструмент рендерингу, а НЕ звірена напряму з
сирим HTML. Перед продакшн-запуском: `python -m scrapers.freelancehunt` і
звір, чи знаходяться картки проєктів; якщо 0 — онови CARD_SELECTOR/BID_RE
за реальним HTML (Chrome DevTools → Inspect).

ТІЛЬКИ дві категорії (config.FREELANCEHUNT_CATEGORIES) — пошук за довільним
ключовим словом через /search/ заблокований robots.txt, не використовується.
"""
from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from config import FREELANCEHUNT_CATEGORIES
from models import RawFreelanceProject
from scrapers.base import ScraperError, fetch, get_session

# Перша сторінка кожної категорії, без пагінації — узгоджено з рештою
# скраперів пайплайна (жоден поки не гортає сторінки, див. README).
COLLECTION_METHOD = "перша_сторінка"

# Картка проєкту на Freelancehunt ідентифікується посиланням виду
# /projects/<slug>/<id>.html
PROJECT_LINK_RE = re.compile(r"^/projects/[\w\-]+/(\d+)\.html")
# "12 ставок" / "12 откликов" — і укр, і рос локалізація трапляється залежно
# від мовних налаштувань акаунту/сесії.
BIDS_RE = re.compile(r"(\d+)\s*(?:ставок|ставк[аиу]|отклик[а-я]*)", re.I)


def _parse_card(card, category: str) -> RawFreelanceProject | None:
    link = card.find("a", href=PROJECT_LINK_RE)
    if link is None:
        return None
    href = link["href"]
    url = "https://freelancehunt.com" + href if href.startswith("/") else href
    title = link.get_text(strip=True)

    full_text = card.get_text(" ", strip=True)

    bids_count: int | None = None
    bids_match = BIDS_RE.search(full_text)
    if bids_match:
        bids_count = int(bids_match.group(1))

    # Бюджет часто не вказаний або "за домовленістю" — семантику "не
    # вказано" визначає Claude в оцінці (Крок 1.2 навмисно НЕ фільтрує за
    # бюджетом, конкуренція за кількістю ставок важливіша, див. config.py).
    return RawFreelanceProject(
        source="freelancehunt.com",
        title=title,
        url=url,
        category=category,
        posted_raw="",  # немає окремого стабільного селектора дати — йде у snippet
        bids_count=bids_count,
        budget_raw="",
        description_snippet=full_text[:600],
    )


def scrape() -> Iterator[RawFreelanceProject]:
    session = get_session()
    seen_urls: set[str] = set()
    any_success = False
    last_error: Exception | None = None

    for category, listing_url in FREELANCEHUNT_CATEGORIES.items():
        try:
            resp = fetch(session, listing_url)
        except ScraperError as exc:
            last_error = exc
            continue
        any_success = True
        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.find_all("a", href=PROJECT_LINK_RE):
            card = link.find_parent(["li", "div", "article"]) or link.parent
            project = _parse_card(card, category)
            if project is None or project.url in seen_urls:
                continue
            seen_urls.add(project.url)
            yield project

    if not any_success:
        raise ScraperError(f"freelancehunt.com: усі категорії провалились ({last_error})")


if __name__ == "__main__":
    for p in scrape():
        print(p)

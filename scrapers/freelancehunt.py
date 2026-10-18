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

Два шляхи збору (25.09.2026):
1. API v2 (основний) — якщо в .env є FREELANCEHUNT_API_TOKEN. JSON, без
   парсингу HTML і без антибот-блокувань.
2. HTML-скрапінг категорій (фолбек без токена) — через curl_cffi, бо
   звичайний requests отримує HTTP 403.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator

from bs4 import BeautifulSoup

from config import (
    FREELANCEHUNT_API_TOKEN,
    FREELANCEHUNT_API_URL,
    FREELANCEHUNT_CATEGORIES,
    FREELANCEHUNT_SKILL_IDS,
)
from models import RawFreelanceProject
from scrapers.base import ScraperError, fetch, get_session

logger = logging.getLogger(__name__)

# Перша сторінка кожної категорії, без пагінації — узгоджено з рештою
# скраперів пайплайна (жоден поки не гортає сторінки, див. README).
COLLECTION_METHOD = "перша_сторінка"

# Картка проєкту на Freelancehunt ідентифікується посиланням виду
# /projects/<slug>/<id>.html
PROJECT_LINK_RE = re.compile(r"^/projects/[\w\-]+/(\d+)\.html")
# "12 ставок" / "12 откликов" — і укр, і рос локалізація трапляється залежно
# від мовних налаштувань акаунту/сесії.
BIDS_RE = re.compile(r"(\d+)\s*(?:ставок|ставк[аиу]|отклик[а-я]*)", re.I)
# Бюджет у картці HTML-фолбеку («5 000 UAH», «200 $»), щоб Правило К працювало
# і без API-токена (раніше budget_raw у фолбеку був порожній завжди).
BUDGET_RE = re.compile(r"(\d[\d\s\u00a0]{0,9})\s*(UAH|грн|₴|USD|\$)", re.I)
# Статус кожної категорії окремо (метрики фрілансу, колонки 6 і 7).
LAST_CATEGORY_STATUS: dict[str, str] = {}


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

    budget_raw = ""
    bm = BUDGET_RE.search(full_text)
    if bm:
        amount = re.sub(r"[\s\u00a0]", "", bm.group(1))
        currency = "USD" if bm.group(2).upper() in ("$", "USD") else "UAH"
        budget_raw = f"{amount} {currency}"
    return RawFreelanceProject(
        source="freelancehunt.com",
        title=title,
        url=url,
        category=category,
        posted_raw="",  # немає окремого стабільного селектора дати — йде у snippet
        bids_count=bids_count,
        budget_raw=budget_raw,
        description_snippet=full_text[:600],
    )


def _budget_str(budget) -> str:
    if not isinstance(budget, dict) or budget.get("amount") in (None, ""):
        return ""
    return f"{budget.get('amount')} {budget.get('currency', '')}".strip()


def _parse_api_item(item: dict, category: str) -> RawFreelanceProject | None:
    """Один елемент `data[]` відповіді API v2 (формат JSON:API:
    {id, attributes: {name, description, budget, bid_count, published_at},
    links: {self: {web}}})."""
    attrs = item.get("attributes") or {}
    title = (attrs.get("name") or "").strip()
    links = item.get("links") or {}
    self_link = links.get("self")
    url = self_link.get("web", "") if isinstance(self_link, dict) else ""
    if not url and item.get("id"):
        url = f"https://freelancehunt.com/project/{item['id']}.html"
    if not title or not url:
        return None
    bid_count = attrs.get("bid_count")
    description = attrs.get("description") or ""
    return RawFreelanceProject(
        source="freelancehunt.com",
        title=title,
        url=url,
        category=category,
        posted_raw=str(attrs.get("published_at") or ""),
        bids_count=int(bid_count) if isinstance(bid_count, (int, float)) else None,
        budget_raw=_budget_str(attrs.get("budget")),
        description_snippet=description[:600],
    )


def _scrape_api(token: str) -> Iterator[RawFreelanceProject]:
    session = get_session()
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
    seen_urls: set[str] = set()
    any_success = False
    last_error: Exception | None = None
    for category, skill_id in FREELANCEHUNT_SKILL_IDS.items():
        try:
            resp = fetch(session, FREELANCEHUNT_API_URL, params={"filter[skill_id]": skill_id})
            payload = resp.json()
        except (ScraperError, ValueError) as exc:
            last_error = exc
            LAST_CATEGORY_STATUS[category] = "недоступна"
            continue
        any_success = True
        LAST_CATEGORY_STATUS[category] = "OK"
        for item in payload.get("data", []) or []:
            project = _parse_api_item(item, category)
            if project is None or project.url in seen_urls:
                continue
            seen_urls.add(project.url)
            yield project
    if not any_success:
        raise ScraperError(f"freelancehunt.com (API): усі категорії провалились ({last_error})")


def scrape() -> Iterator[RawFreelanceProject]:
    """API, якщо є токен; якщо API впало повністю (невалідний токен -> 401,
    мережа) — фолбек на HTML-скрапінг, щоб джерело не губилось через токен."""
    LAST_CATEGORY_STATUS.clear()
    if FREELANCEHUNT_API_TOKEN:
        try:
            projects = list(_scrape_api(FREELANCEHUNT_API_TOKEN))
        except ScraperError as exc:
            logger.warning("Freelancehunt API недоступне (%s) — фолбек на HTML. Перевір FREELANCEHUNT_API_TOKEN у .env", exc)
        else:
            yield from projects
            return
    yield from _scrape_html()


def _scrape_html() -> Iterator[RawFreelanceProject]:
    session = get_session(impersonate=True)
    seen_urls: set[str] = set()
    any_success = False
    last_error: Exception | None = None

    for category, listing_url in FREELANCEHUNT_CATEGORIES.items():
        try:
            resp = fetch(session, listing_url)
        except ScraperError as exc:
            last_error = exc
            LAST_CATEGORY_STATUS[category] = "недоступна"
            continue
        any_success = True
        LAST_CATEGORY_STATUS[category] = "OK"
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

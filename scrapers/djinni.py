"""Скрапер djinni.co.

⚠️ ВАЖЛИВО ПРО НАДІЙНІСТЬ СЕЛЕКТОРІВ: пісочниця, в якій писався цей код, не
має вихідного доступу до інтернету (перевірено — запити на djinni.co падають
на рівні проксі, HTTP 403). CSS-селектори нижче побудовані на основі того,
що вдалося побачити через окремий інструмент рендерингу сторінок під час
розробки (16.09.2026), а НЕ звірені напряму з сирим HTML. Перед першим
продакшн-запуском:
    1. Запусти `python -m scrapers.djinni` окремо і подивись, чи знайдено
       хоч якісь картки вакансій.
    2. Якщо 0 — відкрий сторінку в браузері, знайди реальний клас job-картки
       (Chrome DevTools → Inspect) і онови SELECTOR_* нижче.

Djinni повертає server-rendered HTML (перевірено раніше в сесії розробки),
тому requests+BeautifulSoup має працювати без headless-браузера.
"""
from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from models import RawJobPosting
from scrapers.base import ScraperError, fetch, get_session

# Крок 4 (4.2) — фіксує, ЯК фактично зібрана ця цифра: тут завжди
# "перша_сторінка", бо скрапер бере лише статичні keyword-сторінки нижче,
# без пагінації (гортання сторінок наразі не реалізоване жодним скрапером
# пайплайна) — див. README, "Відомі обмеження".
COLLECTION_METHOD = "перша_сторінка"

BASE_URL = "https://djinni.co"
# Статичні keyword-сторінки Djinni — стабільніші за пошук з query-параметрами
# (перевірено: exp_level у query string не завжди застосовується без сесії
# браузера, тоді як keyword-URL повертає релевантну стрічку одразу).
LISTING_URLS = [
    f"{BASE_URL}/jobs/keyword-data_analyst/",
    f"{BASE_URL}/jobs/keyword-product_analyst/",
]

# Job-картки на Djinni ідентифікуються стабільним патерном URL: /jobs/<id>-<slug>/
JOB_LINK_RE = re.compile(r"^/jobs/(\d+)-[\w-]+/?$")


def _parse_card(card) -> RawJobPosting | None:
    link = card.find("a", href=JOB_LINK_RE)
    if link is None:
        return None
    href = link["href"]
    url = BASE_URL + href if href.startswith("/") else href
    title = link.get_text(strip=True)

    # Компанія зазвичай у сусідньому елементі з класом, що містить "company"
    company_el = card.find(class_=re.compile("company", re.I))
    company = company_el.get_text(strip=True) if company_el else ""

    # Дата/досвід/зарплата — все, що є текстом картки, віддаємо як snippet,
    # а семантичний розбір ("3d ago" -> дата, "2 years" -> досвід) робить
    # Claude, а не регулярки тут.
    full_text = card.get_text(" ", strip=True)

    return RawJobPosting(
        source="djinni.co",
        title=title,
        company=company,
        url=url,
        posted_raw="",  # немає окремого стабільного селектора — йде в snippet
        salary_raw="",
        description_snippet=full_text[:600],
    )


def scrape() -> Iterator[RawJobPosting]:
    session = get_session()
    seen_urls: set[str] = set()
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
        # Картка вакансії — будь-який контейнер, що містить посилання на /jobs/<id>-...
        for link in soup.find_all("a", href=JOB_LINK_RE):
            card = link.find_parent(["li", "div", "article"]) or link.parent
            job = _parse_card(card)
            if job is None or job.url in seen_urls:
                continue
            seen_urls.add(job.url)
            yield job

    if not any_success:
        raise ScraperError(f"djinni.co: усі спроби провалились ({last_error})")


if __name__ == "__main__":
    for j in scrape():
        print(j)

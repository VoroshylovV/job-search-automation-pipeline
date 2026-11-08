"""Скрапер djinni.co.

Розмітку звірено з сирим HTML 08.11.2026 (збережено в tmp/djinni_list_page1.html
і tmp/djinni_job_856537.html; пісочниця не має прямого доступу до djinni.co,
HTML отримано через Firecrawl rawHtml). Факти:
  * картка = div.job-item#job-item-<id>; посилання на вакансію АБСОЛЮТНЕ
    (https://djinni.co/jobs/<id>-<slug>/), не /jobs/...;
  * у картці немає дати публікації, лише «updated 3d ago» / «оновлено 3 дн. тому»
    (span, tooltip data-bs-original-title = «09:45 05.11.2026»);
  * дата публікації — на сторінці вакансії: JSON-LD "datePosted" та текст
    «Published 3 November» / «Опубліковано 4 листопада» (без року);
  * ?page=N зберігає фільтри (page=2 того ж запиту дав 1 картку, не загальну стрічку).
Межа пагінації — за датою оновлення картки (tooltip, інакше «updated X ago»);
точна дата публікації береться з HTML сторінки вакансії, який і так завантажує
detail.fetch_full_text (окремого запиту немає).

Djinni повертає server-rendered HTML (перевірено раніше в сесії розробки),
тому requests+BeautifulSoup має працювати без headless-браузера.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from config import DATE_WINDOW_DAYS, MAX_LISTING_PAGES
from models import RawJobPosting
from scrapers.base import _UA_MONTHS, Coverage, ScraperError, age_days, fetch, get_session

logger = logging.getLogger(__name__)

# Крок 4 (4.2): спосіб збору тепер рахується за фактом (base.coverage_status)
# з LAST_COVERAGE — покриття кожного запиту останнього scrape().
COLLECTION_METHOD = "усі_сторінки"
LAST_COVERAGE: list[Coverage] = []

BASE_URL = "https://djinni.co"
# Статичні keyword-сторінки Djinni — стабільніші за пошук з query-параметрами
# (перевірено: exp_level у query string не завжди застосовується без сесії
# браузера, тоді як keyword-URL повертає релевантну стрічку одразу).
# Фільтри на рівні запиту (додано 02.10.2026): лише remote і досвід
# "без досвіду"/"1 рік". До цього бралась уся стрічка за ключовим словом,
# і офісні/Senior-вакансії потрапляли на оцінку, а картка списку не
# містила достатньо тексту, щоб їх відсіяти. Перевірено 02.10.2026: на
# keyword-data_analyst фільтр звужує видачу до ~9 вакансій.
LISTING_FILTER = "?employment=remote&exp_level=no_exp&exp_level=1y"
# 18.10.2026 (діагностика на ПК + Firecrawl): keyword-product_analyst і
# параметр "all-keywords" (дефіс) Djinni ігнорує — віддає загальну стрічку
# (Media Buyer тощо). Працює лише "all_keywords" (підкреслення).
# 08.11.2026: твердження «page=2+ скидає фільтри» не підтвердилось — page=2
# keyword-data_analyst із фільтрами віддала 1 картку (а не ~400 сторінок
# загальної стрічки). Тому знову гортаємо ?page=N до виходу за вікно.
# Страховка: якщо на page>=2 раптом з'являється стрічка без фільтрів,
# спрацює MAX_LISTING_PAGES (джерело отримає «неповне покриття», не мовчки OK).
LISTING_URLS = [
    f"{BASE_URL}/jobs/keyword-data_analyst/{LISTING_FILTER}",
    f"{BASE_URL}/jobs/?all_keywords=product+analyst&employment=remote&exp_level=no_exp&exp_level=1y",
    f"{BASE_URL}/jobs/?all_keywords=ai+analyst&employment=remote&exp_level=no_exp&exp_level=1y",
    f"{BASE_URL}/jobs/?all_keywords=crm+analyst&employment=remote&exp_level=no_exp&exp_level=1y",
]
# Повна сторінка Djinni — 15 карток. Менше — видача закінчилась (покриття повне).
DJINNI_PAGE_SIZE = 15


def _page_url(url: str, n: int) -> str:
    return url if n == 1 else f"{url}{'&' if '?' in url else '?'}page={n}"

# Посилання на вакансію: /jobs/<id>-<slug>/ — відносне або абсолютне (djinni.co).
JOB_LINK_RE = re.compile(r"^(?:https?://(?:www\.)?djinni\.co)?/jobs/(\d+)-[\w-]+/?$")
# «updated 3d ago» / «оновлено 3 дн. тому» — відносний час оновлення в картці.
UPDATED_RE = re.compile(r"(?:updated|оновлено)\s+[^\n]{1,30}", re.I)

# tooltip на «updated 3d ago»: data-bs-original-title="09:45 05.11.2026"
UPDATED_TOOLTIP_RE = re.compile(r"^(\d{2}):(\d{2}) (\d{2})\.(\d{2})\.(\d{4})$")

_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
PUBLISHED_RE = re.compile(r"(?:published|опубліковано)\s+(\d{1,2})\s+([A-Za-zА-Яа-яІіЇїЄєҐґ]+)(?:\s+(\d{4}))?", re.I)
DATE_POSTED_RE = re.compile(r'"datePosted"\s*:\s*"(\d{4})-(\d{2})-(\d{2})')


def _month_number(word: str) -> Optional[int]:
    w = word.lower()
    return _EN_MONTHS.get(w[:3]) if w.isascii() else next(
        (v for k, v in _UA_MONTHS.items() if w.startswith(k)), None)


def parse_published_date(html: str, today: Optional[date] = None) -> Optional[date]:
    """Дата публікації зі сторінки вакансії. Спершу JSON-LD "datePosted" (повна
    дата з роком), інакше текст «Published 3 November» / «Опубліковано 4 листопада»:
    рік = поточний, а якщо дата в майбутньому — минулий. None — не знайдено."""
    today = today or datetime.now().date()
    m = DATE_POSTED_RE.search(html)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    m = PUBLISHED_RE.search(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    if not m:
        return None
    month = _month_number(m[2])
    if not month:
        return None
    year = int(m[3]) if m[3] else today.year
    try:
        d = date(year, month, int(m[1]))
    except ValueError:
        return None
    if not m[3] and d > today:
        d = d.replace(year=year - 1)
    return d


def _parse_card(card, link) -> RawJobPosting | None:
    href = link["href"]
    url = BASE_URL + href if href.startswith("/") else href
    h2 = card.find(["h2", "h3"], class_=re.compile("position", re.I))
    title = (h2 or link).get_text(" ", strip=True)

    # header span.small — назва компанії (08.11.2026); клас "...company..." є також у
    # порожнього логотипу (is-company-logo), тому він лише запасний варіант.
    company_el = card.select_one("header span.small") or card.find(class_=re.compile("company", re.I))
    company = company_el.get_text(strip=True) if company_el else ""

    full_text = card.get_text(" ", strip=True)
    # posted_raw тут — час ОНОВЛЕННЯ (не публікації). Спершу точний tooltip
    # «HH:MM DD.MM.YYYY» (-> ISO), інакше відносне «updated/оновлено X тому».
    # Точну дату публікації дає сторінка вакансії (detail.fetch_full_text).
    updated_raw = ""
    tip = card.find(attrs={"data-bs-original-title": UPDATED_TOOLTIP_RE})
    if tip is not None:
        t = UPDATED_TOOLTIP_RE.match(tip["data-bs-original-title"].strip())
        updated_raw = f"{t[5]}-{t[4]}-{t[3]}T{t[1]}:{t[2]}"
    else:
        m = UPDATED_RE.search(full_text)
        if m:
            updated_raw = re.sub(r"\s+", " ", m.group(0)).strip()

    return RawJobPosting(
        source="djinni.co",
        title=title,
        company=company,
        url=url,
        posted_raw=updated_raw,
        salary_raw="",
        description_snippet=full_text[:600],
    )


def _parse_page(html: str) -> list[RawJobPosting]:
    soup = BeautifulSoup(html, "html.parser")
    jobs, seen = [], set()
    for link in soup.find_all("a", href=JOB_LINK_RE):
        # Картка — div.job-item; посилання "More"/коментарі на неї не ведуть
        # (JOB_LINK_RE вимагає саме /jobs/<id>-slug/ без #fragment/query).
        card = link.find_parent(class_="job-item")
        if card is None:
            continue
        job = _parse_card(card, link)
        if job is not None and job.url not in seen:
            seen.add(job.url)
            jobs.append(job)
    return jobs


def _paginate_by_updated(session, listing_url: str, today: date) -> tuple[list[RawJobPosting], Coverage]:
    """Гортає ?page=N, поки на сторінці є хоч одна картка, оновлена у вікні
    DATE_WINDOW_DAYS (порядок видачі НЕ монотонний: 3d, 11d, 1d на одній сторінці).
    Зупинка: перша сторінка без жодної картки у вікні, неповна/порожня сторінка
    (покриття повне) або MAX_LISTING_PAGES (неповне). Картка без дати оновлення
    -> попередження з URL, а не мовчазна зупинка."""
    jobs: list[RawJobPosting] = []
    seen: set[str] = set()
    oldest: Optional[int] = None
    pages = 0
    for n in range(1, MAX_LISTING_PAGES + 1):
        try:
            page_jobs = _parse_page(fetch(session, _page_url(listing_url, n)).text)
        except ScraperError:
            if n == 1:
                raise
            logger.warning("djinni.co: сторінка %d недоступна (%s) — покриття неповне", n, listing_url)
            return jobs, Coverage(listing_url, pages, False, oldest)
        pages = n
        new = [j for j in page_jobs if j.url not in seen]
        if not new:
            return jobs, Coverage(listing_url, pages, True, oldest)
        ages: list[int] = []
        for j in new:
            seen.add(j.url)
            jobs.append(j)
            a = age_days(j.posted_raw, today)
            if a is None:
                logger.warning("djinni.co: не знайдено дату оновлення на картці %s (posted_raw=%r)", j.url, j.posted_raw)
            else:
                ages.append(a)
        if ages:
            oldest = max(ages) if oldest is None else max(oldest, max(ages))
        else:
            logger.warning("djinni.co: на сторінці %d (%s) жодної дати картки — межу пагінації визначити не можна",
                           n, _page_url(listing_url, n))
        if ages and not any(a < DATE_WINDOW_DAYS for a in ages):
            return jobs, Coverage(listing_url, pages, True, oldest)  # жодної картки у вікні
        if len(page_jobs) < DJINNI_PAGE_SIZE:
            return jobs, Coverage(listing_url, pages, True, oldest)  # остання сторінка
    return jobs, Coverage(listing_url, pages, False, oldest)


def scrape() -> Iterator[RawJobPosting]:
    session = get_session()
    today = datetime.now().date()
    seen_urls: set[str] = set()
    any_success = False
    last_error: Exception | None = None
    LAST_COVERAGE.clear()

    for listing_url in LISTING_URLS:
        try:
            jobs, cov = _paginate_by_updated(session, listing_url, today)
        except ScraperError as exc:
            last_error = exc
            continue
        any_success = True
        LAST_COVERAGE.append(cov)
        for job in jobs:
            if job.url in seen_urls:
                continue
            seen_urls.add(job.url)
            yield job

    if not any_success:
        raise ScraperError(f"djinni.co: усі спроби провалились ({last_error})")


if __name__ == "__main__":
    for j in scrape():
        print(j)

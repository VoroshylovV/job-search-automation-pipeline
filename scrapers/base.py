"""Спільна HTTP-інфраструктура для всіх скраперів."""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

try:  # curl_cffi опційний: без нього пайплайн працює як раніше (чистий requests)
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - залежить від оточення
    cffi_requests = None

logger = logging.getLogger(__name__)

# Профіль браузера для curl_cffi. robota.ua, work.ua і freelancehunt.com
# відсікають звичайний `requests` за TLS/HTTP2-відбитком (HTTP 403 навіть із
# браузерним User-Agent — підтверджено запуском 25.09.2026). curl_cffi
# відтворює справжній відбиток Chrome, тому ці сайти бачать "браузер".
IMPERSONATE_PROFILE = "chrome"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}


class ScraperError(Exception):
    """Джерело недоступне або повернуло помилку — не фатально для пайплайну.

    pipeline/step1_vacancies.py ловить це виключення й помічає джерело як
    "недоступне" замість того, щоб впасти повністю (той самий принцип
    "обробки помилок доступу до джерел", що був у текстовому промпті).
    """


def get_session(impersonate: bool = False):
    """Повертає requests-сумісну сесію.

    impersonate=True -> curl_cffi з відбитком Chrome (для сайтів з
    антибот-захистом). User-Agent тут НЕ підміняємо: curl_cffi сам ставить
    узгоджений із TLS-відбитком UA, а чужий UA якраз і видає бота.
    Якщо curl_cffi не встановлено — тихий фолбек на requests + попередження.
    """
    if impersonate:
        if cffi_requests is not None:
            session = cffi_requests.Session(impersonate=IMPERSONATE_PROFILE)
            session.headers.update({"Accept-Language": DEFAULT_HEADERS["Accept-Language"]})
            return session
        logger.warning(
            "curl_cffi не встановлено — фолбек на requests (очікуй HTTP 403 на "
            "robota.ua/work.ua/freelancehunt). Встанови: pip install -r requirements.txt"
        )
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def fetch(
    session,
    url: str,
    *,
    params: Optional[dict] = None,
    timeout: int = 15,
    retries: int = 2,
    backoff_seconds: float = 1.5,
):
    """GET з ретраями. Кидає ScraperError, якщо всі спроби провалились —
    відповідає правилу з промпту "одна повторна спроба перед тим, як
    визнати джерело недоступним" (тут — до `retries` спроб).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp
            last_exc = ScraperError(f"{url} -> HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001,PERF203 - requests і curl_cffi мають різні ієрархії помилок
            last_exc = exc
        if attempt < retries:
            time.sleep(backoff_seconds)
    raise ScraperError(str(last_exc)) from last_exc


# --------------------------------------------------------------------------
# Пагінація і покриття джерел (Правило И п.8 онлайн-промпту, 18.10.2026):
# джерело вважається повністю пройденим, лише якщо перегляд видачі дійшов
# до вакансій, старших за вікно свіжості (2 дні), або видача закінчилась.
# Інакше — статус «неповне покриття (N сторінок, найстаріша — X днів)».
# --------------------------------------------------------------------------
import re as _re
from dataclasses import dataclass as _dataclass
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta
from typing import Callable

_UA_MONTHS = {
    "січ": 1, "лют": 2, "бер": 3, "квіт": 4, "трав": 5, "черв": 6,
    "лип": 7, "серп": 8, "верес": 9, "жовт": 10, "листоп": 11, "груд": 12,
}


@_dataclass
class Coverage:
    query: str
    pages: int
    complete: bool
    oldest_days: Optional[int] = None


def age_days(posted_raw: str, today: _date) -> Optional[int]:
    """Вік вакансії в днях за текстом дати з картки видачі; None — не розпізнано.
    Формати: ISO («2026-10-16T…»), «сьогодні»/«вчора», «N днів/годин тому»,
    «22 вересня 2026» / «18 жовтня» (без року — поточний або минулий рік)."""
    t = (posted_raw or "").strip().lower()
    if not t:
        return None
    m = _re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return (today - _date(int(m[1]), int(m[2]), int(m[3]))).days
    if "сьогодні" in t or "today" in t or "годин" in t or "хвилин" in t or "hour" in t:
        return 0
    if "вчора" in t or "yesterday" in t:
        return 1
    m = _re.search(r"(\d+)\s*(дн|день|day)", t)
    if m:
        return int(m[1])
    m = _re.search(r"(\d{1,2})\s+([а-яіїєґ]+)(?:\s+(\d{4}))?", t)
    if m:
        month = next((v for k, v in _UA_MONTHS.items() if m[2].startswith(k)), None)
        if month:
            year = int(m[3]) if m[3] else today.year
            try:
                d = _date(year, month, int(m[1]))
            except ValueError:
                return None
            if not m[3] and d > today:
                d = d.replace(year=year - 1)
            return (today - d).days
    return None


def paginate(
    fetch_page: Callable[[int], list],
    query: str,
    *,
    min_pages: int = 1,
    max_pages: int = 5,
    window_days: int = 2,
    today: Optional[_date] = None,
) -> tuple[list, Coverage]:
    """Гортає сторінки 1..max_pages. Зупиняється, коли (а) сторінка не дала
    нових вакансій (видача закінчилась), або (б) після min_pages на сторінці є
    вакансія, старша за window_days днів. Якщо зупинились лише через
    max_pages — покриття неповне. Помилка на сторінці >1 теж = неповне
    покриття (зібране не губиться); помилка на сторінці 1 — ScraperError."""
    today = today or _datetime.now().date()
    jobs: list = []
    seen: set[str] = set()
    oldest: Optional[int] = None
    pages = 0
    for n in range(1, max_pages + 1):
        try:
            page_jobs = fetch_page(n)
        except ScraperError:
            if n == 1:
                raise
            return jobs, Coverage(query, pages, False, oldest)
        pages = n
        new = [j for j in page_jobs if j.url not in seen]
        if not new:
            return jobs, Coverage(query, pages, True, oldest)
        for j in new:
            seen.add(j.url)
            jobs.append(j)
            a = age_days(getattr(j, "posted_raw", ""), today)
            if a is not None:
                oldest = a if oldest is None else max(oldest, a)
        if n >= min_pages and oldest is not None and oldest >= window_days:
            return jobs, Coverage(query, pages, True, oldest)
    return jobs, Coverage(query, pages, False, oldest)


def coverage_status(coverages: list[Coverage]) -> tuple[str, str]:
    """(статус джерела, спосіб збору для Кроку 4.2)."""
    if not coverages:
        return "OK", "не_встановлено"
    incomplete = [c for c in coverages if not c.complete]
    if not incomplete:
        return "OK", "усі_сторінки"
    pages = max(c.pages for c in incomplete)
    olds = [c.oldest_days for c in incomplete if c.oldest_days is not None]
    oldest = f"{max(olds)} днів" if olds else "невідомо"
    return f"неповне покриття ({pages} сторінок, найстаріша переглянута — {oldest})", "оцінка"

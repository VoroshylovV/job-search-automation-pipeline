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

"""Спільна HTTP-інфраструктура для всіх скраперів."""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

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


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def fetch(
    session: requests.Session,
    url: str,
    *,
    params: Optional[dict] = None,
    timeout: int = 15,
    retries: int = 2,
    backoff_seconds: float = 1.5,
) -> requests.Response:
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
        except requests.RequestException as exc:  # noqa: PERF203
            last_exc = exc
        if attempt < retries:
            time.sleep(backoff_seconds)
    raise ScraperError(str(last_exc)) from last_exc

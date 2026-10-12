"""Повний текст вакансії для другого етапу оцінки (додано 02.10.2026).

Чому це потрібно: картка зі списку видачі (description_snippet, ≤600 символів)
майже ніколи не містить вимог до досвіду й формату роботи. Аналіз логів
відсіву 29.09–02.10.2026 показав, що з такої картки Claude пропускав офісні
вакансії й вакансії з вимогою 2+ роки: та сама вакансія COMFY «Product
Analyst (APP)» була показана з robota.ua (картка: «Київ | …») і відсіяна з
work.ua (повний текст: «досвід від 2 років»).

Тому кожна вакансія, що пройшла перший етап (картку), перед показом
перевіряється за повним текстом сторінки. Повертає None, якщо текст
отримати не вдалось — тоді step1 оцінює за карткою в суворому режимі
(формат/досвід мають бути підтверджені явно, інакше вакансія відсіюється).
"""
from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from models import RawJobPosting
from scrapers.base import ScraperError, fetch, get_session

logger = logging.getLogger(__name__)

FULL_TEXT_MAX_CHARS = 6000

# Сайти з антибот-захистом за TLS-відбитком — див. scrapers/base.py.
_IMPERSONATE_SOURCES = {"robota.ua", "work.ua"}

_ROBOTA_ID_RE = re.compile(r"/vacancy(\d+)")
ROBOTA_VACANCY_API = "https://api.robota.ua/vacancy"

# Основний контент сторінки вакансії; якщо жоден не знайдено — береться весь
# <body> без службових блоків. Селектори — найімовірніші контейнери опису на
# кожному джерелі; помилка селектора не фатальна (fallback на body).
_MAIN_SELECTORS = {
    "jobs.dou.ua": ["div.l-vacancy", "div.b-vacancy"],
    "work.ua": ["div#job-description", "div.card.wordwrap"],
    "djinni.co": ["main", "div.job-post-page"],
    # happymonday.ua: перевірка 02.10.2026 показала, що "main"/"article"
    # повертають усю сторінку з шапкою сайту («Work with Ukraine…», «Останнє
    # оновлення…»), а опис вакансії обрізався лімітом 6000 символів. Тому
    # для нього — лише евристика від заголовка вакансії (_from_title_block).
    "happymonday.ua": [],
}
# Джерела, де текст ріжеться від заголовка вакансії (h1), а не від початку.
_TITLE_ANCHORED_SOURCES = {"happymonday.ua"}
_MIN_BLOCK_CHARS = 800
_NOISE_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "form", "svg", "iframe"]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _from_title_block(soup) -> str | None:
    """Найменший предок заголовка вакансії (h1), що містить ≥_MIN_BLOCK_CHARS
    символів тексту, і текст, обрізаний так, щоб починався з самого
    заголовка — шапка сайту й меню до нього відкидаються."""
    h1 = soup.find("h1")
    if h1 is None:
        return None
    title = _clean(h1.get_text(" ", strip=True))
    node = h1
    while node.parent is not None and len(node.get_text(strip=True)) < _MIN_BLOCK_CHARS:
        node = node.parent
    text = _clean(node.get_text(" ", strip=True))
    if title and title in text:
        text = text[text.index(title):]
    return text if len(text) > 200 else None


def _html_to_text(html: str, source: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    for selector in _MAIN_SELECTORS.get(source, []):
        node = soup.select_one(selector)
        if node is not None and len(node.get_text(strip=True)) > 200:
            return _clean(node.get_text(" ", strip=True))
    if source in _TITLE_ANCHORED_SOURCES:
        block = _from_title_block(soup)
        if block:
            return block
    body = soup.body or soup
    return _clean(body.get_text(" ", strip=True))


def _robota_full_text(session, url: str) -> str | None:
    """robota.ua — Angular-оболонка без тексту в HTML, тому опис береться з
    того самого публічного API, що й видача: /vacancy?id=… (поля
    description, clusters із «Видом зайнятості», cityName, date)."""
    m = _ROBOTA_ID_RE.search(url)
    if not m:
        return None
    payload = fetch(session, ROBOTA_VACANCY_API, params={"id": m.group(1)}).json()
    if not isinstance(payload, dict):
        return None
    description = re.sub(r"<[^>]+>", " ", str(payload.get("description") or ""))
    clusters = json.dumps(payload.get("clusters") or [], ensure_ascii=False)
    parts = [
        f"Місто: {payload.get('cityName') or ''}",
        f"Адреса: {payload.get('vacancyAddress') or ''}",
        f"Дата публікації: {payload.get('date') or ''}",
        f"Кластери (вид зайнятості тощо): {clusters}",
        f"Опис: {description}",
    ]
    return _clean(" | ".join(parts))


def fetch_full_text(job: RawJobPosting) -> str | None:
    """Повний текст вакансії (≤FULL_TEXT_MAX_CHARS) або None."""
    if not job.url:
        return None
    try:
        session = get_session(impersonate=job.source in _IMPERSONATE_SOURCES)
        if job.source == "robota.ua":
            text = _robota_full_text(session, job.url)
        else:
            text = _html_to_text(fetch(session, job.url).text, job.source)
    except (ScraperError, ValueError) as exc:
        # Текст помилки може містити сире API-посилання robota.ua — у лог не йде.
        detail = "помилка API" if job.source == "robota.ua" else exc
        logger.warning("Повний текст %s недоступний: %s", job.url, detail)
        return None
    except Exception as exc:  # noqa: BLE001 - різні HTTP-бекенди мають різні помилки
        logger.warning("Повний текст %s: неочікувана помилка %s", job.url, exc)
        return None
    if not text or len(text) < 200:
        return None
    return text[:FULL_TEXT_MAX_CHARS]

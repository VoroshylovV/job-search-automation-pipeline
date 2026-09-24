"""Скрапер публічного веб-прев'ю Telegram-каналу для Кроку 1.2 (джерело 3).

Використовує https://t.me/s/<channel> — публічну HTML-версію стрічки
каналу, без логіну й без окремого Telegram Bot API конектора. Показує
стрічку останніх постів з часом публікації; глибина стрічки на цьому
ендпоінті обмежена (типово останні ~20 постів), що для вікна свіжості
"останні 1-2 дні" (Крок 1.2) зазвичай достатньо.

⚠️ Та сама застереження про непере вірені селектори, що й в інших
скраперах пайплайна (scrapers/djinni.py, scrapers/freelancehunt.py) —
онови TG_MESSAGE_SELECTOR/TG_LINK_RE за реальним HTML перед продакшеном.

На відміну від Freelancehunt, тут НЕМАЄ категорій і НЕМАЄ кількості
ставок/відгуків — фільтр релевантності виключно за ключовими словами в
тексті поста (config.TELEGRAM_RELEVANCE_KEYWORDS), а не структурою
сторінки. Дата поста теж читається як текст ("HH:MM" або повна дата) —
семантичний розбір "сьогодні/вчора" робить Claude, як і для решти джерел.
"""
from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from config import TELEGRAM_FREELANCE_CHANNEL, TELEGRAM_FREELANCE_PREVIEW_URL
from models import RawFreelanceProject
from scrapers.base import ScraperError, fetch, get_session

COLLECTION_METHOD = "перша_сторінка"  # глибина стрічки обмежена самим ендпоінтом t.me/s/

# Кожен пост у веб-прев'ю — блок з постійним посиланням виду
# /freelance_jobs_motivation/<id>
POST_LINK_RE = re.compile(rf"^/{re.escape(TELEGRAM_FREELANCE_CHANNEL)}/(\d+)$")


def _parse_post(block) -> RawFreelanceProject | None:
    link = block.find("a", href=POST_LINK_RE)
    if link is None:
        return None
    href = link["href"]
    url = "https://t.me" + href if href.startswith("/") else href

    text_el = block.find(class_=re.compile(r"js-message_text|tgme_widget_message_text", re.I))
    text = text_el.get_text(" ", strip=True) if text_el else block.get_text(" ", strip=True)

    time_el = block.find("time")
    posted_raw = (time_el.get("datetime") or time_el.get_text(strip=True)) if time_el else ""

    # Заголовка як такого немає (це потік постів, не оголошень з назвою) —
    # перші ~80 символів тексту слугують заголовком у звіті, повний текст
    # лишається в description_snippet.
    title = (text[:80] + "…") if len(text) > 80 else text

    return RawFreelanceProject(
        source="telegram",
        title=title or "(без тексту)",
        url=url,
        category="Telegram",
        posted_raw=posted_raw,
        bids_count=None,
        budget_raw="",
        description_snippet=text[:600],
    )


def scrape() -> Iterator[RawFreelanceProject]:
    session = get_session()
    try:
        resp = fetch(session, TELEGRAM_FREELANCE_PREVIEW_URL)
    except ScraperError as exc:
        raise ScraperError(f"t.me/s/{TELEGRAM_FREELANCE_CHANNEL}: {exc}") from exc

    soup = BeautifulSoup(resp.text, "html.parser")
    seen_urls: set[str] = set()
    for link in soup.find_all("a", href=POST_LINK_RE):
        block = link.find_parent(class_=re.compile(r"tgme_widget_message\b", re.I)) or link.find_parent(
            ["div"]
        )
        post = _parse_post(block)
        if post is None or post.url in seen_urls:
            continue
        seen_urls.add(post.url)
        yield post


if __name__ == "__main__":
    for p in scrape():
        print(p)

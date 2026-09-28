"""Розбір публічної стрічки t.me/s/<канал>: id, дата, перегляди, текст поста.

Текст поста живе лише в пам'яті до класифікації — у файли він не пишеться.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

_VIEWS_RE = re.compile(r"^([\d.,]+)\s*([KkMm]?)$")


@dataclass
class Post:
    channel: str
    post_id: int
    datetime: str  # ISO з атрибута <time datetime>, UTC
    views: int | None
    text: str


def parse_views(raw: str) -> int | None:
    m = _VIEWS_RE.match((raw or "").strip())
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(round(num * mult))


def _external_links(el) -> list[str]:
    """Посилання з тексту поста (get_text їх губить). Профілі t.me — це контакти, їх не беремо."""
    out = []
    for a in el.select("a[href]"):
        href = a["href"]
        if href.startswith("http") and "t.me/" not in href and href not in out:
            out.append(href)
    return out


def parse_page(html: str, channel: str) -> list[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts: list[Post] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        data_post = msg.get("data-post", "")
        try:
            post_id = int(data_post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        time_el = msg.select_one("a.tgme_widget_message_date time[datetime]") or msg.select_one("time[datetime]")
        if time_el is None:
            continue
        text_el = msg.select_one("div.tgme_widget_message_text")
        links = _external_links(text_el) if text_el else []
        views_el = msg.select_one("span.tgme_widget_message_views")
        posts.append(Post(
            channel=channel,
            post_id=post_id,
            datetime=time_el["datetime"],
            views=parse_views(views_el.get_text(strip=True)) if views_el else None,
            text=(text_el.get_text("\n", strip=True) if text_el else "") + (
                "\nПосилання: " + " ".join(links) if links else ""),
        ))
    return posts

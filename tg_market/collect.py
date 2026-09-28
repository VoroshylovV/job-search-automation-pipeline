"""Збір і класифікація постів з каналів панелі.

    python -m tg_market.collect --since 2026-08-01            # основна панель
    python -m tg_market.collect --since 2026-08-01 --panel all

Результат (у tg_market/data/, поза git):
    posts.csv      — рівень поста: канал, id, дата, перегляди, чи вакансія, скільки вакансій
    vacancies.csv  — рівень вакансії: витягнуті поля (без тексту поста й контактів)
    state.json     — останній оброблений post_id по каналу (повторний запуск бере лише нове)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_orchestrator.client import call_json
from scrapers.base import ScraperError, fetch, get_session
from tg_market.classify import build_prompt, normalize_vacancy
from tg_market.parse import Post, parse_page

logger = logging.getLogger("tg_market")
BASE = Path(__file__).parent
DATA = BASE / "data"
CHUNK = 20
PAUSE_SECONDS = 1.5  # ввічлива пауза між сторінками t.me

POST_FIELDS = ["channel", "post_id", "datetime", "views", "is_vacancy", "vacancy_count", "collected_at"]
VAC_FIELDS = ["channel", "post_id", "datetime", "vacancy_idx", "title", "company", "is_it", "direction",
              "level", "experience_years_min", "skills", "work_format", "remote_scope", "salary_stated",
              "salary_min", "salary_max", "salary_currency", "link"]


def load_channels(panel: str) -> list[dict]:
    with open(BASE / "channels.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows if panel == "all" else [r for r in rows if r["panel"] == panel]


def fetch_channel(session, handle: str, since: datetime, min_id: int, max_pages: int) -> list[Post]:
    """Гортає стрічку назад (?before=) до дати since або до вже обробленого id."""
    out: list[Post] = []
    before: int | None = None
    for _ in range(max_pages):
        params = {"before": before} if before else None
        resp = fetch(session, f"https://t.me/s/{handle}", params=params)
        page = parse_page(resp.text, handle)
        if not page:
            break
        page.sort(key=lambda p: p.post_id)
        stop = False
        for p in page:
            if p.post_id <= min_id or datetime.fromisoformat(p.datetime) < since:
                stop = True
                continue
            out.append(p)
        if stop or page[0].post_id == before:
            break
        before = page[0].post_id
        time.sleep(PAUSE_SECONDS)
    return sorted({p.post_id: p for p in out}.values(), key=lambda p: p.post_id)


def classify(posts: list[Post]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for i in range(0, len(posts), CHUNK):
        chunk = posts[i:i + CHUNK]
        data = call_json(build_prompt(chunk), max_tokens=12000)
        for item in data.get("posts", []):
            result[str(item.get("key"))] = item
    return result


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="YYYY-MM-DD, найраніша дата постів")
    ap.add_argument("--panel", default="core", choices=["core", "secondary", "all"])
    ap.add_argument("--channel", help="лише один канал (для перевірки)")
    ap.add_argument("--dry-run", action="store_true", help="лише зібрати й порахувати пости, без Claude і без запису")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    DATA.mkdir(exist_ok=True)
    state_path = DATA / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    channels = load_channels(args.panel)
    if args.channel:
        channels = [c for c in channels if c["handle"] == args.channel] or [{"handle": args.channel, "max_pages": "40"}]

    session = get_session()
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for ch in channels:
        handle = ch["handle"]
        try:
            posts = fetch_channel(session, handle, since, int(state.get(handle, 0)), int(ch.get("max_pages") or 40))
        except ScraperError as exc:
            logger.warning("%s: недоступний (%s)", handle, exc)
            continue
        span = f"{posts[0].datetime[:10]} … {posts[-1].datetime[:10]}" if posts else "—"
        logger.info("%s: %d нових постів (%s)", handle, len(posts), span)
        if args.dry_run or not posts:
            continue

        parsed = classify(posts)
        post_rows, vac_rows = [], []
        for p in posts:
            item = parsed.get(f"{handle}/{p.post_id}", {})
            vacs = [normalize_vacancy(v) for v in (item.get("vacancies") or [])] if item.get("is_vacancy") else []
            post_rows.append({"channel": handle, "post_id": p.post_id, "datetime": p.datetime, "views": p.views,
                              "is_vacancy": bool(vacs), "vacancy_count": len(vacs), "collected_at": collected_at})
            for idx, v in enumerate(vacs):
                vac_rows.append({"channel": handle, "post_id": p.post_id, "datetime": p.datetime, "vacancy_idx": idx, **v})
        append_csv(DATA / "posts.csv", POST_FIELDS, post_rows)
        append_csv(DATA / "vacancies.csv", VAC_FIELDS, vac_rows)
        state[handle] = posts[-1].post_id
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        logger.info("%s: записано %d постів, %d вакансій", handle, len(post_rows), len(vac_rows))


if __name__ == "__main__":
    main()

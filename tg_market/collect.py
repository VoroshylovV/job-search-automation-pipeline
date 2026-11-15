"""Збір і класифікація постів з каналів панелі.

    python -m tg_market.collect --since 2026-08-01            # основна панель
    python -m tg_market.collect --since 2026-08-01 --panel all
    python -m tg_market.collect --since 2026-08-01 --panel all --backfill-raw   # один раз: текст уже зібраних постів

Результат (у tg_market/data/, поза git):
    runs.csv       — один рядок на запуск: модель, версія промпта, пости, токени, вартість
    posts.csv      — рівень поста: канал, id, дата, перегляди, чи вакансія, скільки вакансій
    vacancies.csv  — рівень вакансії: витягнуті поля (без тексту поста й контактів)
    raw/<канал>.jsonl — повний текст постів (лише на диску: не в таблицях і не на Drive)
    state.json     — останній оброблений post_id по каналу (повторний запуск бере лише нове)

Ритм: один запуск на місяць із сталим --since (наприклад 2026-08-01); state.json
не дає брати старе вдруге. Значення --since записується в runs.csv.

Захист від тихих втрат (15.11.2026):
  * канал, чия стрічка не догорнута до попереднього запуску за max_pages сторінок, НЕ записується
    і state.json для нього не зсувається («неповне покриття») — інакше проміжок зник би назавжди;
  * порожня перша сторінка t.me — це «канал недоступний», а не «0 нових постів»;
  * пости, які модель пропустила у відповіді, подаються ще раз; довгі пости йдуть окремим запитом повним текстом;
  * ліміт вартості запуску TG_MARKET_BUDGET_USD (типово $6; 0 — без ліміту);
  * обірваний запис каналу (падіння між файлами) прибирається при наступному запуску.
Код завершення: 0 — усе зібрано; 2 — частково (є проблемні канали); 3 — зупинено за лімітом вартості.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_orchestrator.client import ClaudeBudgetExceededError, ClaudeCallError, call_json
from claude_orchestrator.cost import tracker
from scrapers.base import ScraperError, fetch, get_session
from tg_market import runlog
from tg_market.classify import LONG_POST_MAX_CHARS, PROMPT, SHORT_POST_CHARS, build_prompt, normalize_vacancy
from tg_market.parse import Post, parse_page

logger = logging.getLogger("tg_market")
BASE = Path(__file__).parent
DATA = BASE / "data"
CHUNK = 10  # дайджести з 10+ вакансій у пості роздувають відповідь — менші порції
PAUSE_SECONDS = 1.5  # ввічлива пауза між сторінками t.me
# Модель лише для tg_market; решта пайплайну лишається на config.CLAUDE_MODEL.
# Перебити можна змінною TG_MARKET_MODEL у .env.
TG_MARKET_MODEL = os.environ.get("TG_MARKET_MODEL", "claude-haiku-5-5")
RETRY_CHUNK = 5              # порція для повторної спроби постів, яких не було у відповіді моделі
UNCLASSIFIED_ALERT_SHARE = 0.02   # понад 2% нерозібраних постів — запуск «частковий»
EXIT_PARTIAL, EXIT_BUDGET = 2, 3


def budget_limit() -> float | None:
    """Стеля вартості одного запуску збору, USD. TG_MARKET_BUDGET_USD=0 вимикає ліміт."""
    raw = os.environ.get("TG_MARKET_BUDGET_USD", "6").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"TG_MARKET_BUDGET_USD={raw!r}: потрібне число (наприклад 6 або 0)") from None
    return value if value > 0 else None

POST_FIELDS = runlog.POST_FIELDS
VAC_FIELDS = runlog.VAC_FIELDS


def load_channels(panel: str) -> list[dict]:
    with open(BASE / "channels.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows if panel == "all" else [r for r in rows if r["panel"] == panel]


def fetch_channel(session, handle: str, since: datetime, min_id: int, max_pages: int) -> tuple[list[Post], str | None]:
    """Гортає стрічку назад (?before=) до дати since або до вже обробленого id.

    Повертає (пости, проблема). Проблема не None, якщо за max_pages сторінок стрічка не дійшла ні до since,
    ні до min_id, ні до початку каналу: тоді між зібраним і попереднім запуском лишився проміжок.
    Порожня перша сторінка — ScraperError: канал, який є в панелі, не може не мати жодного поста."""
    out: list[Post] = []
    before: int | None = None
    complete = False
    for _ in range(max_pages):
        params = {"before": before} if before else None
        resp = fetch(session, f"https://t.me/s/{handle}", params=params)
        page = parse_page(resp.text, handle)
        if not page:
            if before is None:
                raise ScraperError(f"порожня стрічка: t.me/s/{handle} не віддав жодного поста")
            complete = True      # сторінка раніше першого поста — початок каналу
            break
        page.sort(key=lambda p: p.post_id)
        stop = False
        for p in page:
            if p.post_id <= min_id or datetime.fromisoformat(p.datetime) < since:
                stop = True
                continue
            out.append(p)
        if stop or page[0].post_id == before:
            complete = True
            break
        before = page[0].post_id
        time.sleep(PAUSE_SECONDS)
    posts = sorted({p.post_id: p for p in out}.values(), key=lambda p: p.post_id)
    problem = None
    if not complete:
        oldest = posts[0].datetime[:10] if posts else "—"
        problem = (f"неповне покриття: {max_pages} сторінок не дійшли до попереднього запуску "
                   f"(найстаріший зібраний пост {oldest}); повтори з --channel {handle} --max-pages {max_pages * 3}")
    return posts, problem


def _classify_chunk(chunk: list[Post], result: dict[str, dict], model: str | None = None,
                    limit: int = SHORT_POST_CHARS) -> None:
    """Якщо відповідь обірвалась (невалідний JSON) — ділимо порцію навпіл.
    Один пост, який не вдалося розібрати, пропускаємо з позначкою, а не валимо весь запуск.
    Вичерпаний ліміт вартості не ділиться і не ковтається: він зупиняє запуск."""
    try:
        data = call_json(build_prompt(chunk, limit), max_tokens=16000, model=model or TG_MARKET_MODEL,
                         budget_limit=budget_limit())
    except ClaudeBudgetExceededError:
        raise
    except ClaudeCallError as exc:
        if len(chunk) == 1:
            logger.warning("%s/%s: не вдалося розібрати (%s) — пропущено", chunk[0].channel, chunk[0].post_id, exc)
            return
        mid = len(chunk) // 2
        _classify_chunk(chunk[:mid], result, model, limit)
        _classify_chunk(chunk[mid:], result, model, limit)
        return
    expected = {f"{p.channel}/{p.post_id}" for p in chunk}
    for item in data.get("posts", []) if isinstance(data, dict) else []:
        key = str(item.get("key")) if isinstance(item, dict) else ""
        if key in expected:          # ключ, якого не було в запиті, — вигадка моделі, не беремо
            result[key] = item
        else:
            logger.warning("модель повернула ключ %r, якого не було в запиті — відкинуто", key)


def classify(posts: list[Post], model: str | None = None) -> dict[str, dict]:
    """Короткі пости — порціями по CHUNK; довгі (дайджести) — по одному, повним текстом.
    Пости, яких немає у відповіді моделі, подаються ще раз меншими порціями (одна повторна спроба)."""
    result: dict[str, dict] = {}
    short = [p for p in posts if len(p.text) <= SHORT_POST_CHARS]
    long_posts = [p for p in posts if len(p.text) > SHORT_POST_CHARS]
    for i in range(0, len(short), CHUNK):
        _classify_chunk(short[i:i + CHUNK], result, model)
    for p in long_posts:
        _classify_chunk([p], result, model, LONG_POST_MAX_CHARS)
    missing = [p for p in posts if f"{p.channel}/{p.post_id}" not in result]
    if missing:
        logger.warning("%s: %d постів без відповіді моделі — повторна спроба", missing[0].channel, len(missing))
        for i in range(0, len(missing), RETRY_CHUNK):
            part = missing[i:i + RETRY_CHUNK]
            limit = LONG_POST_MAX_CHARS if any(len(p.text) > SHORT_POST_CHARS for p in part) else SHORT_POST_CHARS
            _classify_chunk(part, result, model, limit)
    return result


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    runlog.append_csv(path, fields, rows)


def backfill_raw(session, channels: list[dict], since: datetime, state: dict, data: Path) -> dict:
    """Дозбирає сирий текст для постів, які вже є в posts.csv (зібрані до появи raw/).
    Без Claude, без змін posts/vacancies/state/runs: лише raw/<канал>.jsonl. Повертає звіт по каналах:
    скільки збережено і скількох постів із posts.csv у стрічці вже немає (видалені)."""
    known: dict[str, set[int]] = {}
    posts_csv = data / "posts.csv"
    if posts_csv.exists():
        with open(posts_csv, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                known.setdefault(r["channel"], set()).add(int(r["post_id"]))
    report = {}
    for ch in channels:
        handle = ch["handle"]
        want = known.get(handle, set())
        if not want:
            continue
        try:
            posts, _problem = fetch_channel(session, handle, since, 0, int(ch.get("max_pages") or 40))
        except ScraperError as exc:
            logger.warning("%s: недоступний (%s)", handle, exc)
            report[handle] = {"saved": 0, "missing": len(want), "error": str(exc)}
            continue
        got = [p for p in posts if p.post_id in want]
        saved = runlog.save_raw(data / "raw", "backfill", got)
        report[handle] = {"saved": saved, "missing": len(want) - len({p.post_id for p in got})}
        logger.info("%s: дозбір тексту — збережено %d, у стрічці немає %d з %d", handle, saved,
                    report[handle]["missing"], len(want))
    return report


def _token_totals() -> dict:
    sts = tracker.stages.values()
    return {"calls": sum(st.calls for st in sts), "input": sum(st.input_tokens for st in sts),
            "output": sum(st.output_tokens for st in sts), "cache_read": sum(st.cache_read_tokens for st in sts),
            "cache_write": sum(st.cache_write_tokens for st in sts)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="YYYY-MM-DD, найраніша дата постів")
    ap.add_argument("--panel", default="core", choices=["core", "secondary", "reference", "all"])
    ap.add_argument("--channel", help="лише один канал (для перевірки)")
    ap.add_argument("--max-pages", type=int, help="скільки сторінок стрічки гортати (замість max_pages із channels.csv)")
    ap.add_argument("--dry-run", action="store_true", help="лише зібрати й порахувати пости, без Claude і без запису")
    ap.add_argument("--backfill-raw", action="store_true",
                    help="дозібрати лише сирий текст уже оброблених постів (без Claude, без змін CSV і state.json)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    DATA.mkdir(exist_ok=True)
    state_path = DATA / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    channels = load_channels(args.panel)
    if args.channel:
        channels = [c for c in channels if c["handle"] == args.channel] or [{"handle": args.channel, "max_pages": "40"}]

    if args.backfill_raw:
        report = backfill_raw(get_session(), channels, since, state, DATA)
        total_saved = sum(r["saved"] for r in report.values())
        total_missing = sum(r["missing"] for r in report.values())
        logger.info("дозбір тексту: збережено %d постів, у стрічці вже немає %d", total_saved, total_missing)
        return

    limit = budget_limit()
    if not args.dry_run:
        recovered = runlog.recover_pending(DATA)
        if recovered:
            logger.warning("попередній запуск обірвався під час запису каналу %s: прибрано %d постів і %d вакансій "
                           "(копії .bak), канал буде зібрано заново", recovered["channel"], recovered["posts"],
                           recovered["vacancies"])

    prev = runlog.previous_since(DATA / "runs.csv")
    if prev and prev != args.since and not args.dry_run:
        logger.warning("--since %s відрізняється від попереднього запуску (%s): місяці порівнюються лише при сталому --since",
                       args.since, prev)

    tracker.reset()
    run_id = runlog.new_run_id()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    totals = {"posts": 0, "classified": 0, "vacancy_posts": 0, "vacancies": 0}
    done_channels: list[str] = []
    problems: list[str] = []
    status = "ok"

    session = get_session()
    collected_at = started_at
    try:
        for ch in channels:
            handle = ch["handle"]
            max_pages = args.max_pages or int(ch.get("max_pages") or 40)
            try:
                posts, problem = fetch_channel(session, handle, since, int(state.get(handle, 0)), max_pages)
            except ScraperError as exc:
                logger.warning("%s: недоступний (%s)", handle, exc)
                status = "partial"
                problems.append(f"{handle}: недоступний ({exc})")
                continue
            if problem:
                # нічого не пишемо і state не зсуваємо: інакше проміжок між запусками зник би назавжди
                logger.error("%s: %s", handle, problem)
                status = "partial"
                problems.append(f"{handle}: {problem}")
                continue
            span = f"{posts[0].datetime[:10]} … {posts[-1].datetime[:10]}" if posts else "—"
            logger.info("%s: %d нових постів (%s)", handle, len(posts), span)
            if args.dry_run or not posts:
                continue

            runlog.save_raw(DATA / "raw", run_id, posts)   # текст зберігаємо до класифікації: збій Claude його не губить
            try:
                parsed = classify(posts)
            except ClaudeBudgetExceededError as exc:
                logger.error("%s: %s — запуск зупинено, канал не записано (буде зібрано наступним запуском)", handle, exc)
                status = "budget"
                problems.append(f"{handle} і наступні канали: ліміт вартості ${limit}")
                break
            post_rows, vac_rows = [], []
            for p in posts:
                key = f"{handle}/{p.post_id}"
                item = parsed.get(key, {})
                vacs = [normalize_vacancy(v) for v in (item.get("vacancies") or []) if isinstance(v, dict)] \
                    if item.get("is_vacancy") else []
                post_rows.append({"run_id": run_id, "channel": handle, "post_id": p.post_id, "datetime": p.datetime,
                                  "views": p.views, "classified": key in parsed, "is_vacancy": bool(vacs),
                                  "vacancy_count": len(vacs), "collected_at": collected_at})
                for idx, v in enumerate(vacs):
                    vac_rows.append({"run_id": run_id, "channel": handle, "post_id": p.post_id,
                                     "datetime": p.datetime, "vacancy_idx": idx, **v})
            unclassified = sum(1 for r in post_rows if not r["classified"])
            if unclassified:
                logger.warning("%s: %d із %d постів модель не розібрала (classified=False)", handle, unclassified, len(post_rows))
            # три файли пишуться під позначкою pending.json: падіння між ними прибирається наступним запуском
            runlog.begin_commit(DATA, run_id, handle)
            append_csv(DATA / "vacancies.csv", VAC_FIELDS, vac_rows)
            append_csv(DATA / "posts.csv", POST_FIELDS, post_rows)
            state[handle] = posts[-1].post_id
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            runlog.finish_commit(DATA)
            done_channels.append(handle)
            totals["posts"] += len(post_rows)
            totals["classified"] += len(post_rows) - unclassified
            totals["vacancy_posts"] += sum(1 for r in post_rows if r["is_vacancy"])
            totals["vacancies"] += len(vac_rows)
            logger.info("%s: записано %d постів, %d вакансій", handle, len(post_rows), len(vac_rows))
        lost = totals["posts"] - totals["classified"]
        if totals["posts"] and lost / totals["posts"] > UNCLASSIFIED_ALERT_SHARE:
            problems.append(f"нерозібраних постів {lost} із {totals['posts']} (понад {UNCLASSIFIED_ALERT_SHARE:.0%})")
            if status == "ok":
                status = "partial"
    except BaseException:
        status = "failed"
        raise
    finally:
        if not args.dry_run and (totals["posts"] or status != "ok"):
            row = runlog.build_run_row(
                run_id=run_id, started_at=started_at, since=args.since, panel=args.channel and "channel" or args.panel,
                channels=done_channels, model=TG_MARKET_MODEL, prompt_text=PROMPT, posts=totals["posts"],
                classified=totals["classified"], vacancy_posts=totals["vacancy_posts"], vacancies=totals["vacancies"],
                tokens=_token_totals(), cost_usd=tracker.total_usd, status=status, problems=problems, budget_limit=limit)
            append_csv(DATA / "runs.csv", runlog.RUN_FIELDS, [row])
            logger.info("run %s: %d постів, $%.4f ($%s за 1000), статус %s, runs.csv оновлено", run_id, totals["posts"],
                        tracker.total_usd, row["cost_per_1000_posts_usd"], status)
    for line in problems:
        logger.warning("ПРОБЛЕМА: %s", line)
    if status == "budget":
        sys.exit(EXIT_BUDGET)
    if status == "partial":
        sys.exit(EXIT_PARTIAL)


if __name__ == "__main__":
    main()

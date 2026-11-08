"""Облік запусків збору (runs.csv), сталі схеми CSV і сирий текст постів на диску.

runs.csv — один рядок на запуск collect: модель, версія промпта, кількість постів,
токени, вартість. Це головний документ для порівняння моделей і звірки з білінгом.
raw/<канал>.jsonl — повний текст постів (поза git і поза Drive): з ним зміну
класифікатора перевіряють без повторного збору.

Схема кожного CSV фіксована (SCHEMA_VERSION). Якщо на диску старіша шапка —
файл один раз переписується з порожніми значеннями нових колонок (копія .bak).
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
SOURCE = "telegram"  # ключ джерела: окремий від сайтів вакансій у спільних звітах

POST_FIELDS = ["run_id", "channel", "post_id", "datetime", "views", "classified", "is_vacancy",
               "vacancy_count", "collected_at"]
VAC_FIELDS = ["run_id", "channel", "post_id", "datetime", "vacancy_idx", "title", "company", "is_it",
              "direction", "level", "experience_years_min", "skills", "work_format", "remote_scope",
              "salary_stated", "salary_min", "salary_max", "salary_currency", "salary_period", "link"]
RUN_FIELDS = ["run_id", "source", "schema_version", "started_at", "finished_at", "since", "panel",
              "channels", "model", "prompt_hash", "git_hash", "git_dirty", "posts", "classified_posts",
              "vacancy_posts", "vacancies", "api_calls", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_write_tokens", "cost_usd", "cost_per_post_usd",
              "cost_per_1000_posts_usd", "status"]


def new_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def prompt_hash(prompt_text: str) -> str:
    """Короткий відбиток тексту промпта: змінився промпт — змінився відбиток, навіть без коміту."""
    return hashlib.sha1(prompt_text.encode("utf-8")).hexdigest()[:10]


def git_info(repo: Path | None = None) -> tuple[str, bool]:
    """(короткий хеш HEAD, чи є незакомічені зміни). Без git — ('unknown', False)."""
    cwd = str(repo or Path(__file__).parent)
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "tg_market", "claude_orchestrator"],
                                    cwd=cwd, capture_output=True, text=True, timeout=10, check=True).stdout.strip())
        return head or "unknown", dirty
    except (OSError, subprocess.SubprocessError):
        return "unknown", False


def ensure_schema(path: Path, fields: list[str]) -> None:
    """Якщо файл існує зі старою шапкою — переписує його під `fields` (копія .bak).
    Невідомі колонки не губляться мовчки: виняток."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        if header == fields:
            return
        extra = [c for c in header if c not in fields]
        if extra:
            raise ValueError(f"{path.name}: невідомі колонки {extra} — схему не змінюю автоматично")
        rows = list(reader)
    if any(None in r for r in rows):
        raise ValueError(f"{path.name}: є рядки з більшою кількістю полів, ніж у шапці — перевір файл вручну")
    shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    ensure_schema(path, fields)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="raise")
        if new:
            w.writeheader()
        w.writerows(rows)


def previous_since(path: Path) -> str | None:
    """`since` останнього запуску з runs.csv (для попередження про зміну ритму)."""
    if not path.exists():
        return None
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-1].get("since") if rows else None


def build_run_row(*, run_id: str, started_at: str, since: str, panel: str, channels: list[str], model: str,
                  prompt_text: str, posts: int, classified: int, vacancy_posts: int, vacancies: int,
                  tokens: dict, cost_usd: float, status: str, git: tuple[str, bool] | None = None) -> dict:
    git_hash, dirty = git or git_info()
    return {
        "run_id": run_id, "source": SOURCE, "schema_version": SCHEMA_VERSION, "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "since": since,
        "panel": panel, "channels": "; ".join(channels), "model": model, "prompt_hash": prompt_hash(prompt_text),
        "git_hash": git_hash, "git_dirty": dirty, "posts": posts, "classified_posts": classified,
        "vacancy_posts": vacancy_posts, "vacancies": vacancies, "api_calls": tokens.get("calls", 0),
        "input_tokens": tokens.get("input", 0), "output_tokens": tokens.get("output", 0),
        "cache_read_tokens": tokens.get("cache_read", 0), "cache_write_tokens": tokens.get("cache_write", 0),
        "cost_usd": round(cost_usd, 4),
        "cost_per_post_usd": round(cost_usd / posts, 6) if posts else "",
        "cost_per_1000_posts_usd": round(1000 * cost_usd / posts, 3) if posts else "",
        "status": status,
    }


def save_raw(raw_dir: Path, run_id: str, posts) -> int:
    """Дописує повний текст нових постів у raw/<канал>.jsonl; уже збережені post_id пропускає.
    Повертає, скільки записано. Каталог поза git (tg_market/data/) і не вивантажується на Drive."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    by_channel: dict[str, list] = {}
    for p in posts:
        by_channel.setdefault(p.channel, []).append(p)
    written = 0
    for channel, items in by_channel.items():
        path = raw_dir / f"{channel}.jsonl"
        known: set[int] = set()
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        known.add(int(json.loads(line)["post_id"]))
                    except (ValueError, KeyError):
                        continue
        with open(path, "a", encoding="utf-8") as f:
            for p in items:
                if p.post_id in known:
                    continue
                f.write(json.dumps({"run_id": run_id, "channel": p.channel, "post_id": p.post_id,
                                    "datetime": p.datetime, "views": p.views, "text": p.text},
                                   ensure_ascii=False) + "\n")
                written += 1
    return written

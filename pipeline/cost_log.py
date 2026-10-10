"""Журнал вартості запусків logs/costs.csv: один рядок на запуск, ДОПИСУЄТЬСЯ
(не перезаписується). Колонки: дата/час UTC, git-коміт, моделі, виклики і
токени + $ окремо по етапах, сума, знайдено/оцінено/показано, статус, а також
показникові якості (показані за рівнями, перетин URL з онлайн-версією) — щоб
довести, що оптимізація не погіршила результат. Журнал рішень — docs/COSTS.md."""
from __future__ import annotations

import csv
import logging
import subprocess
from pathlib import Path

from claude_orchestrator.cost import STAGES, tracker as cost_tracker
from config import COSTS_CSV_PATH

logger = logging.getLogger(__name__)

_STAGE_COLUMNS = ("calls", "in", "out", "cache_read", "cache_write", "usd")
COSTS_HEADER = (
    ["date_utc", "commit", "models"]
    + [f"{stage}_{col}" for stage in STAGES for col in _STAGE_COLUMNS]
    + ["other_usd", "total_usd", "found", "evaluated", "shown", "usd_per_shown", "run_status",
       "shown_high", "shown_medium", "shown_low", "url_overlap_online"]
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def git_commit() -> str:
    """Короткий хеш HEAD (+ "-dirty", якщо є незакомічені зміни поза logs/)."""
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=10, check=True).stdout

    try:
        commit = run("rev-parse", "--short", "HEAD").strip()
        dirty = [line for line in run("status", "--porcelain").splitlines() if not line[3:].startswith("logs/")]
        return commit + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001 - не git-репозиторій / git недоступний
        return "н/д"


def run_status(selfcheck) -> str:
    """OK, якщо всі кроки OK; НЕ ВИКОНАНО, якщо жоден не виконано; інакше ЧАСТКОВО."""
    statuses = [selfcheck.step1_status, selfcheck.step2_status,
                selfcheck.step3_metrics_status, selfcheck.step1_2_status]
    if all(s == "OK" for s in statuses):
        return "OK"
    if all(s == "НЕ ВИКОНАНО" for s in statuses):
        return "НЕ ВИКОНАНО"
    return "ЧАСТКОВО"


def build_row(timestamp_utc: str, step1_result: dict, selfcheck, step5_result: dict | None,
              commit: str | None = None) -> list:
    vacancies = step1_result.get("vacancies", [])
    shown = len(vacancies)
    total = cost_tracker.total_usd

    models = ";".join(
        f"{stage}={'+'.join(sorted(cost_tracker.stage(stage).models))}"
        for stage in STAGES if cost_tracker.stage(stage).models
    )
    row: list = [timestamp_utc, commit if commit is not None else git_commit(), models]
    for stage in STAGES:
        st = cost_tracker.stage(stage)
        row += [st.calls, st.input_tokens, st.output_tokens, st.cache_read_tokens, st.cache_write_tokens, round(st.usd, 5)]
    overlap = "н/д"
    if step5_result and step5_result.get("active"):
        value = getattr(step5_result.get("row"), "v_url_overlap", None)
        overlap = "н/д" if value is None else value
    row += [
        round(cost_tracker.stage("other").usd, 5),
        round(total, 5),
        step1_result.get("total_found_before_filters", 0),
        step1_result.get("evaluated_count", 0),
        shown,
        round(total / shown, 5) if shown else "н/д",
        run_status(selfcheck),
        sum(1 for v in vacancies if v.match_level == "High"),
        sum(1 for v in vacancies if v.match_level == "Medium"),
        sum(1 for v in vacancies if v.match_level == "Low"),
        overlap,
    ]
    return row


def append_cost_row(row: list, path: str | Path | None = None) -> Path:
    """Дописує рядок; заголовок пишеться лише у новий (або порожній) файл."""
    target = Path(path) if path else _REPO_ROOT / COSTS_CSV_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    is_new = not target.exists() or target.stat().st_size == 0
    with open(target, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(COSTS_HEADER)
        writer.writerow(row)
    return target

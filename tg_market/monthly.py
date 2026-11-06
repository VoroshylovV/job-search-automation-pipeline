"""Помісячний звіт: Telegram за напрямами IT + порівняння кількості аналітичних вакансій TG vs репо.

    python -m tg_market.monthly --since 2026-08-01 --logs logs

Одиниця — унікальна вакансія (дедуплікація як в analyze/compare), місяць = перша поява.
Сайти: логи пайплайну; напрям визначено евристикою за назвою (compare.guess_direction), тож
цифри сайтів — оцінка. Логи сайтів існують не за весь період — місяці без даних позначені «—».
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from pathlib import Path

from tg_market.analyze import SMALL_N
from tg_market.classify import DIRECTIONS
from tg_market.compare import load_board, load_tg, write_csv

JUNIOR = {"intern", "junior"}


def ym(d: date) -> str:
    return d.strftime("%Y-%m")


def table(title: str, header: list[str], rows: list[list]) -> None:
    print(f"\n{title}")
    w = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    for r in [header, *rows]:
        print("  " + "  ".join(str(x).rjust(w[i]) if i else str(x).ljust(w[i]) for i, x in enumerate(r)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01")
    ap.add_argument("--until", default=date.today().isoformat())
    ap.add_argument("--logs", default="logs")
    a = ap.parse_args()
    start, end = date.fromisoformat(a.since), date.fromisoformat(a.until)

    tg = load_tg(start, end)
    months = sorted({ym(u["date"]) for u in tg})
    it = [u for u in tg if u["is_it_b"]]

    # 1. TG: IT-вакансії за напрямами і місяцями
    cnt = Counter((u["direction"], ym(u["date"])) for u in it)
    rows, out = [], []
    for d in DIRECTIONS:
        if d == "non-it":
            continue
        per = [cnt[(d, m)] for m in months]
        junior = sum(1 for u in it if u["direction"] == d and u["level"] in JUNIOR)
        rows.append([d, *per, sum(per), junior])
        out.append({"direction": d, **dict(zip(months, per)), "total": sum(per), "junior_intern": junior})
    tot = [sum(cnt[(d, m)] for d in DIRECTIONS if d != "non-it") for m in months]
    rows.append(["УСЬОГО IT", *tot, sum(tot), sum(1 for u in it if u["level"] in JUNIOR)])
    all_m = [sum(1 for u in tg if ym(u["date"]) == m) for m in months]
    rows.append(["усі вакансії (з non-it)", *all_m, sum(all_m), ""])
    table("Telegram: унікальні IT-вакансії за напрямами (місяць першої появи)",
          ["напрям", *months, "разом", "junior+intern"], rows)
    write_csv("monthly_tg_direction.csv", out)

    # 2. Рівні в аналітиці по місяцях
    lv = []
    for m in months:
        an = [u for u in it if u["direction"] == "analytics" and ym(u["date"]) == m]
        lv.append([m, len(an), sum(1 for u in an if u["level"] in JUNIOR),
                   sum(1 for u in an if u["level"] == "unspecified"),
                   sum(1 for u in an if u["level"] in {"middle", "senior", "lead"})])
    table("Telegram, аналітика за рівнем", ["місяць", "всього", "junior+intern", "не вказано", "middle+"], lv)

    # 3. TG vs репо (сайти) — аналітика
    board, _, _ = load_board(Path(a.logs), start, end)
    bm = sorted({ym(b["date"]) for b in board})
    cmp_rows, cmp_out = [], []
    for m in sorted(set(months) | set(bm)):
        tg_an = [u for u in it if u["direction"] == "analytics" and ym(u["date"]) == m]
        tg_jr = sum(1 for u in tg_an if u["level"] in JUNIOR | {"unspecified"})
        b = [x for x in board if ym(x["date"]) == m]
        has = m in bm
        b_an = [x for x in b if x["direction"] == "analytics"]
        s_an = sum(1 for x in b_an if x["status"] in ("shown", "applied"))
        row = [m, len(tg_an), tg_jr, s_an if has else "—", len(b_an) if has else "—", len(b) if has else "—"]
        cmp_rows.append(row)
        cmp_out.append(dict(zip(["month", "tg_analytics", "tg_analytics_junior_or_unspecified",
                                 "repo_analytics_shown_applied", "repo_analytics_wide", "repo_all_wide"], row)))
    table("Аналітичні вакансії за місяцями: Telegram vs репо (сайти)",
          ["місяць", "TG аналітика", "TG junior/не вказано", "репо показані+подані", "репо wide", "репо усі (wide)"],
          cmp_rows)
    write_csv("monthly_tg_vs_repo.csv", cmp_out)
    print(f"\nМала вибірка: n<{SMALL_N}. Сайти: напрям за евристикою назви; "
          "wide = показані+подані+відсіяні за роль/досвід/формат/ЗП.")
    print("Місяць репо без даних = логів ще не було (reject-лог з 27.09, локальні з 26.10).")


if __name__ == "__main__":
    main()

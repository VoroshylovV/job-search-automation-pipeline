"""Дедуплікація вакансій і розрахунок метрик ринку та каналів.

    python -m tg_market.analyze            # читає tg_market/data/*.csv, пише tg_market/data/out/*.csv

Дві одиниці рахунку:
  * згадка (рядок vacancies.csv) — для метрик каналу;
  * унікальна вакансія — для ринку: та сама посада + компанія, повторена в межах
    DEDUP_WINDOW_DAYS від попередньої згадки (у будь-якому каналі), рахується один раз.
Усі ринкові метрики — частки з n (знаменником) поруч; n < SMALL_N позначається.
"""
from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DATA = Path(__file__).parent / "data"
OUT = DATA / "out"
DEDUP_WINDOW_DAYS = 30
SMALL_N = 30

_LEVEL_WORDS = r"\b(trainee|intern|internship|junior|middle|senior|lead|strong|стажер|стажування)\b"


def norm_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\(.*?\)", " ", t)          # уточнення в дужках
    t = re.sub(_LEVEL_WORDS, " ", t)         # рівень — окреме поле, не частина назви
    t = re.sub(r"[^\w+#.]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_company(company: str) -> str:
    return re.sub(r"\s+", " ", (company or "").lower()).strip()


def _mode(values):
    vals = [v for v in values if v not in ("", "unspecified", None)]
    return Counter(vals).most_common(1)[0][0] if vals else "unspecified"


def load(name: str) -> list[dict]:
    with open(DATA / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def dedup(rows: list[dict]) -> list[dict]:
    """Повертає унікальні вакансії з агрегованими полями."""
    # 1) компанія: якщо порожня — найчастіша компанія цієї ж посади
    by_title = defaultdict(Counter)
    for r in rows:
        if r["company"]:
            by_title[norm_title(r["title"])][norm_company(r["company"])] += 1
    for r in rows:
        r["_title"] = norm_title(r["title"])
        c = norm_company(r["company"])
        r["_company"] = c or (by_title[r["_title"]].most_common(1)[0][0] if by_title[r["_title"]] else "")
        r["_dt"] = datetime.fromisoformat(r["datetime"])

    # 2) кластери: ключ + розрив між сусідніми згадками ≤ вікна
    groups = defaultdict(list)
    for r in rows:
        # без компанії назва надто загальна («SMM Manager» від різних роботодавців) —
        # тоді формат роботи входить у ключ як додаткова ознака
        extra = "" if r["_company"] else r["work_format"]
        groups[(r["_title"], r["_company"], extra)].append(r)
    uniques = []
    for _key, items in groups.items():
        items.sort(key=lambda r: r["_dt"])
        cluster = [items[0]]
        for r in items[1:]:
            if r["_dt"] - cluster[-1]["_dt"] <= timedelta(days=DEDUP_WINDOW_DAYS):
                cluster.append(r)
            else:
                uniques.append(_aggregate(cluster))
                cluster = [r]
        uniques.append(_aggregate(cluster))
    return uniques


def _aggregate(cluster: list[dict]) -> dict:
    first = cluster[0]
    skills = Counter(s.strip() for r in cluster for s in r["skills"].split(";") if s.strip())
    return {
        "title": first["title"],
        "company": first["_company"],
        "first_seen": first["datetime"][:10],
        "last_seen": cluster[-1]["datetime"][:10],
        "mentions": len(cluster),
        "channels": "; ".join(sorted({r["channel"] for r in cluster})),
        "first_channel": first["channel"],
        "is_it": Counter(r["is_it"] for r in cluster).most_common(1)[0][0],
        "direction": _mode(r["direction"] for r in cluster),
        "level": _mode(r["level"] for r in cluster),
        "work_format": _mode(r["work_format"] for r in cluster),
        "salary_stated": any(r["salary_stated"] == "True" for r in cluster),
        "skills": "; ".join(s for s, _ in skills.most_common()),
        "link": next((r["link"] for r in cluster if r["link"]), ""),
    }


def iso_week(date_str: str) -> str:
    y, w, _ = datetime.fromisoformat(date_str).isocalendar()
    return f"{y}-W{w:02d}"


def shares(rows: list[dict], group_key, value_key) -> list[dict]:
    """Частка кожного значення value_key всередині групи group_key, з n."""
    groups = defaultdict(list)
    for r in rows:
        groups[group_key(r)].append(value_key(r))
    out = []
    for g, values in sorted(groups.items()):
        n = len(values)
        for v, cnt in Counter(values).most_common():
            out.append({"group": g, "value": v, "share_pct": round(100 * cnt / n, 1), "n": n,
                        "small_sample": n < SMALL_N})
    return out


def channel_metrics(posts: list[dict], mentions: list[dict], uniques: list[dict]) -> list[dict]:
    by_ch = defaultdict(list)
    for p in posts:
        by_ch[p["channel"]].append(p)
    ment = Counter(m["channel"] for m in mentions)
    it_ment = Counter(m["channel"] for m in mentions if m["is_it"] == "True")
    first = Counter(u["first_channel"] for u in uniques if u["is_it"] == "True")
    exclusive = Counter(u["channels"] for u in uniques if u["is_it"] == "True" and ";" not in u["channels"])
    out = []
    for ch, ps in sorted(by_ch.items()):
        views = [int(p["views"]) for p in ps if p["views"]]
        weeks = max(1, len({iso_week(p["datetime"]) for p in ps}))
        uniq_it = sum(1 for u in uniques if u["is_it"] == "True" and ch in u["channels"].split("; "))
        out.append({
            "channel": ch,
            "posts": len(ps),
            "vacancy_post_share_pct": round(100 * sum(p["is_vacancy"] == "True" for p in ps) / len(ps), 1),
            "mentions": ment[ch],
            "it_share_pct": round(100 * it_ment[ch] / ment[ch], 1) if ment[ch] else None,
            "unique_it_vacancies": uniq_it,
            "unique_it_per_week": round(uniq_it / weeks, 1),
            "repeat_ratio": round(it_ment[ch] / uniq_it, 2) if uniq_it else None,
            "published_first": first[ch],
            "exclusive_share_pct": round(100 * exclusive[ch] / uniq_it, 1) if uniq_it else None,
            "avg_views": round(sum(views) / len(views)) if views else None,
        })
    return out


def write(name: str, rows: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    posts, mentions = load("posts.csv"), load("vacancies.csv")
    uniques = dedup(mentions)
    it = [u for u in uniques if u["is_it"] == "True"]
    week = lambda u: iso_week(u["first_seen"])  # noqa: E731

    write("unique_vacancies.csv", uniques)
    write("channel_metrics.csv", channel_metrics(posts, mentions, uniques))
    write("weekly_direction.csv", shares(it, week, lambda u: u["direction"]))
    write("weekly_level.csv", shares(it, week, lambda u: u["level"]))
    write("weekly_format.csv", shares(it, week, lambda u: u["work_format"]))
    write("direction_level.csv", shares(it, lambda u: u["direction"], lambda u: u["level"]))
    write("direction_format.csv", shares(it, lambda u: u["direction"], lambda u: u["work_format"]))
    write("direction_salary_stated.csv", shares(it, lambda u: u["direction"], lambda u: u["salary_stated"]))
    # скіли: частка вакансій напряму, де скіл згадано (одна вакансія може мати багато скілів)
    skill_rows, by_dir = [], defaultdict(list)
    for u in it:
        by_dir[u["direction"]].append({s.strip() for s in u["skills"].split(";") if s.strip()})
    for d, sets in sorted(by_dir.items()):
        n = len(sets)
        for s, c in Counter(s for st in sets for s in st).most_common(20):
            skill_rows.append({"direction": d, "skill": s, "share_pct": round(100 * c / n, 1), "n": n,
                               "small_sample": n < SMALL_N})
    write("skills_by_direction.csv", skill_rows)
    print(f"згадок: {len(mentions)} → унікальних вакансій: {len(uniques)} (IT: {len(it)}); результати в {OUT}")


if __name__ == "__main__":
    main()

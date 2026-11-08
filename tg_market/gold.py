"""Еталонна вибірка для оцінки класифікатора: вибірка → ручна розмітка → точність/повнота з довірчими інтервалами.

    python -m tg_market.gold sample --n 100            # gold_posts.csv + gold_vacancies.csv для розмітки
    python -m tg_market.gold score                     # оцінка поточної класифікації (vacancies.csv)
    python -m tg_market.gold predict --model <модель>  # прогнати іншу модель по тих самих постах (потрібен API)
    python -m tg_market.gold score --pred tg_market/data/gold/pred_<модель>.csv

Усе лежить у tg_market/data/gold/ (поза git і поза Drive: у gold_posts.csv повний текст постів).

Розмітка (відкрий CSV в Excel/Sheets, значення вписуй рівно як нижче):
  gold_posts.csv      true_is_vacancy: y | n;  true_vacancy_count: ціле число (скільки різних вакансій у пості)
  gold_vacancies.csv  один рядок на вакансію, яку знайшла модель: true_is_it: y | n;
                      true_direction: з classify.DIRECTIONS або none (моделі привиділась вакансія);
                      true_level: з classify.LEVELS. Пропущені моделлю вакансії додай новими рядками
                      (key + true_title + true_* поля).
Розмічай по тексту поста, не дивлячись на відповіді моделі.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from tg_market.analyze import DATA, norm_title
from tg_market.classify import DIRECTIONS, LEVELS, normalize_vacancy

GOLD = DATA / "gold"
POST_FIELDS = ["key", "channel", "datetime", "text", "true_is_vacancy", "true_vacancy_count", "notes"]
VAC_FIELDS = ["key", "title_hint", "true_title", "true_is_it", "true_direction", "true_level", "notes"]
PRED_FIELDS = ["key", "title", "is_it", "direction", "level"]
MATCH_RATIO = 0.6
YES, NO = {"y", "yes", "так", "true", "1"}, {"n", "no", "ні", "false", "0"}


# ---------- допоміжне ----------

def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:  # BOM: Excel коректно відкриває кирилицю
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% інтервал Вілсона для частки k/n."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Коефіцієнт Коена між еталоном і моделлю."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)


def flag(value: str) -> bool | None:
    v = (value or "").strip().lower()
    return True if v in YES else False if v in NO else None


def load_raw(raw_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(raw_dir.glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out[f"{r['channel']}/{r['post_id']}"] = r
    return out


def load_baseline(data: Path) -> tuple[dict[str, bool], dict[str, list[dict]]]:
    """Поточна класифікація з posts.csv/vacancies.csv: {key: is_vacancy}, {key: [вакансії]}."""
    posts = {f"{r['channel']}/{r['post_id']}": r for r in read_csv(data / "posts.csv")}
    classified = {k for k, r in posts.items() if r.get("classified", "True") == "True"}
    vacs: dict[str, list[dict]] = {}
    for r in read_csv(data / "vacancies.csv"):
        vacs.setdefault(f"{r['channel']}/{r['post_id']}", []).append(
            {"key": f"{r['channel']}/{r['post_id']}", "title": r["title"], "is_it": r["is_it"],
             "direction": r["direction"], "level": r["level"]})
    return {k: (bool(vacs.get(k))) for k in classified}, vacs


# ---------- sample ----------

def build_sample(raw: dict[str, dict], is_vac: dict[str, bool], vacs: dict[str, list[dict]], n: int, seed: int,
                 vac_share: float = 0.7) -> tuple[list[dict], list[dict]]:
    """Випадкова (не збалансована) вибірка: частка вакансійних постів vac_share, решта — не вакансії.
    Випадковий добір дає похибку, що відповідає всій зібраній вибірці."""
    keys = sorted(k for k in is_vac if k in raw)
    rng = random.Random(seed)
    yes = [k for k in keys if is_vac[k]]
    no = [k for k in keys if not is_vac[k]]
    rng.shuffle(yes)
    rng.shuffle(no)
    n_yes = min(len(yes), round(n * vac_share))
    chosen = sorted(yes[:n_yes] + no[:max(0, n - n_yes)], key=lambda k: (raw[k]["datetime"], k))
    posts = [{"key": k, "channel": raw[k]["channel"], "datetime": raw[k]["datetime"], "text": raw[k]["text"],
              "true_is_vacancy": "", "true_vacancy_count": "", "notes": ""} for k in chosen]
    vrows = [{"key": k, "title_hint": v["title"], "true_title": "", "true_is_it": "", "true_direction": "",
              "true_level": "", "notes": ""} for k in chosen for v in vacs.get(k, [])]
    return posts, vrows


# ---------- перевірка розмітки ----------

def validate_labels(posts: list[dict], vrows: list[dict]) -> list[str]:
    errs = []
    for i, r in enumerate(posts, start=2):
        if r["true_is_vacancy"].strip() and flag(r["true_is_vacancy"]) is None:
            errs.append(f"gold_posts.csv рядок {i}: true_is_vacancy={r['true_is_vacancy']!r} (треба y/n)")
        c = r["true_vacancy_count"].strip()
        if c and not c.isdigit():
            errs.append(f"gold_posts.csv рядок {i}: true_vacancy_count={c!r} (треба ціле число)")
    for i, r in enumerate(vrows, start=2):
        if r["true_is_it"].strip() and flag(r["true_is_it"]) is None:
            errs.append(f"gold_vacancies.csv рядок {i}: true_is_it={r['true_is_it']!r} (треба y/n)")
        d, lv = r["true_direction"].strip().lower(), r["true_level"].strip().lower()
        if d and d != "none" and d not in DIRECTIONS:
            errs.append(f"gold_vacancies.csv рядок {i}: true_direction={d!r} (допустимо: {', '.join(DIRECTIONS)}, none)")
        if lv and lv not in LEVELS:
            errs.append(f"gold_vacancies.csv рядок {i}: true_level={lv!r} (допустимо: {', '.join(LEVELS)})")
    return errs


# ---------- score ----------

def match(gold: list[dict], pred: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Жадібне зіставлення вакансій одного поста за схожістю назв. Повертає (пари, gold без пари, pred без пари)."""
    cands = []
    for gi, g in enumerate(gold):
        gt = norm_title(g.get("true_title") or g.get("title_hint") or "")
        for pi, p in enumerate(pred):
            cands.append((SequenceMatcher(None, gt, norm_title(p["title"])).ratio(), gi, pi))
    cands.sort(reverse=True)
    used_g, used_p, pairs = set(), set(), []
    for ratio, gi, pi in cands:
        if ratio < MATCH_RATIO or gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        pairs.append((gold[gi], pred[pi]))
    return (pairs, [g for i, g in enumerate(gold) if i not in used_g], [p for i, p in enumerate(pred) if i not in used_p])


def _pct(k: int, n: int) -> str:
    if n == 0:
        return "—"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.0f}% [{100 * lo:.0f}–{100 * hi:.0f}], n={n}"


def score(posts: list[dict], vrows: list[dict], pred_is_vac: dict[str, bool], pred_vacs: dict[str, list[dict]],
          label: str) -> tuple[list[dict], list[dict]]:
    """Метрики й список розбіжностей. Бере лише пости з розміченим true_is_vacancy."""
    labelled = [r for r in posts if flag(r["true_is_vacancy"]) is not None]
    tp = sum(1 for r in labelled if flag(r["true_is_vacancy"]) and pred_is_vac.get(r["key"]))
    fp = sum(1 for r in labelled if not flag(r["true_is_vacancy"]) and pred_is_vac.get(r["key"]))
    fn = sum(1 for r in labelled if flag(r["true_is_vacancy"]) and not pred_is_vac.get(r["key"]))
    keys = {r["key"] for r in labelled}

    gold_by, pred_by = {}, {}
    for r in vrows:
        if r["key"] in keys and any(r[f].strip() for f in ("true_title", "true_is_it", "true_direction", "true_level")):
            gold_by.setdefault(r["key"], []).append(r)
    for k in keys:
        pred_by[k] = pred_vacs.get(k, [])
    pairs, missed, extra = [], [], []
    for k in keys:
        real = [g for g in gold_by.get(k, []) if g["true_direction"].strip().lower() != "none"]
        pr, gm, pe = match(real, pred_by[k])
        pairs += pr
        missed += gm
        extra += pe          # зокрема вакансії, які еталон визнав вигаданими (none), — хибні
    v_tp, v_fn, v_fp = len(pairs), len(missed), len(extra)

    metrics, errors = [], []

    def add(metric: str, k: int, n: int, extra_txt: str = "") -> None:
        lo, hi = wilson(k, n)
        metrics.append({"model": label, "metric": metric, "k": k, "n": n,
                        "value_pct": round(100 * k / n, 1) if n else None,
                        "ci95_low_pct": round(100 * lo, 1) if n else None,
                        "ci95_high_pct": round(100 * hi, 1) if n else None, "note": extra_txt})

    add("post_is_vacancy_precision", tp, tp + fp)
    add("post_is_vacancy_recall", tp, tp + fn)
    add("vacancy_extraction_precision", v_tp, v_tp + v_fp)
    add("vacancy_extraction_recall", v_tp, v_tp + v_fn)
    for field, truth_key in (("is_it", "true_is_it"), ("direction", "true_direction"), ("level", "true_level")):
        ok, pk = 0, []
        for g, p in pairs:
            gv = g[truth_key].strip().lower()
            if not gv:
                continue
            pv = str(p[field]).strip().lower()
            if field == "is_it":
                gv, pv = ("y" if flag(gv) else "n"), ("y" if flag(pv) else "n")
            pk.append((gv, pv))
            if gv == pv:
                ok += 1
            else:
                errors.append({"model": label, "key": g["key"], "title": p["title"], "field": field, "gold": gv, "pred": pv})
        k_ = kappa(pk)
        add(f"{field}_accuracy", ok, len(pk), f"kappa={k_:.2f}" if k_ is not None else "")
    for g in missed:
        errors.append({"model": label, "key": g["key"], "title": g.get("true_title") or g.get("title_hint"),
                       "field": "vacancy_missed", "gold": "є", "pred": "немає"})
    for p in extra:
        errors.append({"model": label, "key": p["key"], "title": p["title"], "field": "vacancy_extra",
                       "gold": "немає", "pred": "є"})
    return metrics, errors


# ---------- CLI ----------

def cmd_sample(a) -> int:
    if (GOLD / "gold_posts.csv").exists() and not a.force:
        print("gold_posts.csv уже існує — розмітку не затираю. Для нової вибірки додай --force (стара розмітка зникне).")
        return 2
    raw = load_raw(DATA / "raw")
    if not raw:
        print("tg_market/data/raw/ порожній: спершу `collect --backfill-raw`.")
        return 1
    is_vac, vacs = load_baseline(DATA)
    posts, vrows = build_sample(raw, is_vac, vacs, a.n, a.seed)
    write_csv(GOLD / "gold_posts.csv", POST_FIELDS, posts)
    write_csv(GOLD / "gold_vacancies.csv", VAC_FIELDS, vrows)
    print(f"вибірка: {len(posts)} постів ({sum(1 for p in posts if is_vac[p['key']])} з вакансіями), "
          f"{len(vrows)} вакансій для розмітки → {GOLD}")
    return 0


def cmd_predict(a) -> int:
    from tg_market import collect
    from tg_market.parse import Post
    from claude_orchestrator.cost import tracker
    posts_rows = read_csv(GOLD / "gold_posts.csv")
    posts = [Post(r["channel"], int(r["key"].rsplit("/", 1)[1]), r["datetime"], None, r["text"]) for r in posts_rows]
    tracker.reset()
    parsed = collect.classify(posts, model=a.model)
    rows = []
    for p in posts:
        item = parsed.get(f"{p.channel}/{p.post_id}", {})
        for v in ([normalize_vacancy(x) for x in (item.get("vacancies") or [])] if item.get("is_vacancy") else []):
            rows.append({"key": f"{p.channel}/{p.post_id}", "title": v["title"], "is_it": v["is_it"],
                         "direction": v["direction"], "level": v["level"]})
    out = GOLD / f"pred_{a.model.replace('/', '_')}.csv"
    write_csv(out, PRED_FIELDS, rows)
    print(f"{a.model}: {len(rows)} вакансій у {len(posts)} постах, ${tracker.total_usd:.4f} → {out}")
    return 0


def cmd_score(a) -> int:
    posts, vrows = read_csv(GOLD / "gold_posts.csv"), read_csv(GOLD / "gold_vacancies.csv")
    errs = validate_labels(posts, vrows)
    if errs:
        print("Помилки в розмітці:\n  " + "\n  ".join(errs[:30]))
        return 2
    n_lab = sum(1 for r in posts if flag(r["true_is_vacancy"]) is not None)
    if n_lab == 0:
        print("Немає розмічених постів (true_is_vacancy порожнє).")
        return 1
    if a.pred:
        pr = read_csv(Path(a.pred))
        pred_vacs: dict[str, list[dict]] = {}
        for r in pr:
            pred_vacs.setdefault(r["key"], []).append(r)
        pred_is_vac = {k: True for k in pred_vacs}
        label = Path(a.pred).stem.removeprefix("pred_")
    else:
        pred_is_vac, pred_vacs = load_baseline(DATA)
        label = "current"
    metrics, errors = score(posts, vrows, pred_is_vac, pred_vacs, label)
    print(f"Розмічено постів: {n_lab} з {len(posts)}; модель/набір: {label}")
    for m in metrics:
        txt = _pct(m["k"], m["n"])
        print(f"  {m['metric']:<32} {txt}  {m['note']}")
    out = DATA / "out"
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / f"eval_metrics_{label}.csv", list(metrics[0]), metrics)
    if errors:
        write_csv(out / f"eval_errors_{label}.csv", list(errors[0]), errors)
        print(f"  розбіжностей: {len(errors)} → {out / f'eval_errors_{label}.csv'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--force", action="store_true")
    p = sub.add_parser("predict")
    p.add_argument("--model", required=True)
    c = sub.add_parser("score")
    c.add_argument("--pred", help="CSV передбачень (gold predict); без нього — поточна класифікація")
    a = ap.parse_args(argv)
    return {"sample": cmd_sample, "predict": cmd_predict, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())

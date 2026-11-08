"""Порівняння Telegram-ринку з вакансіями із сайтів (логи щоденного пошуку) під профіль кандидата.

    python -m tg_market.compare                      # попередній календарний місяць, logs/ у корені репо
    python -m tg_market.compare --month 2026-10 --logs logs

Питання: якби я шукав роботу лише в Telegram-каналах, скільки вакансій із сайтів я б не побачив?
Сторона сайтів — логи пайплайну (logs/*.csv, колонки REJECT_LOG_HEADER). Сторона Telegram —
tg_market/data/vacancies.csv. Вакансії зіставляються за URL, назвою+компанією (з допусками).
Через евристику збігів усі частки — діапазон «нижня–верхня межа», а не одне число.

Профіль (константи нижче) відтворює жорсткі критерії пошуку: junior-рівень, remote, поріг ЗП,
досвід до 1 року формального, стоп-технології й виключені домени. Англійська мова в Telegram-даних
не витягується, тому цей фільтр до Telegram не застосовується (межа методу).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from tg_market.analyze import DATA, OUT, SMALL_N, dedup, load, norm_company, norm_title, normalize_skills, reclassify

# ----------------------------------------------------------------------------- профіль
FIT_DIRECTIONS = {"analytics"}
ACCEPT_LEVELS = {"intern", "junior", "unspecified"}   # middle і вище — відсів за «досвід»
MAX_EXPERIENCE_YEARS = 1                               # вимога 2+ років формального досвіду — відсів
ACCEPT_FORMATS = {"remote", "unspecified"}             # hybrid/office — відсів за «формат»
SALARY_FLOOR_MONTH = {"UAH": 30_000, "USD": 700, "EUR": 650}  # EUR — груба оцінка, без конвертації між валютами
MY_SKILLS = {"sql", "postgresql", "bigquery", "python", "pandas", "numpy", "scipy", "tableau", "looker studio",
             "excel", "google sheets", "a/b testing", "dbt", "git", "statistics"}
LEARNING_SKILLS = {"power bi"}                         # вивчає — не заявлений навик
REJECT_SKILLS = {"bpmn", "uml", "1c", "1с", "oracle", "oracle sql", "dax"}  # жорсткі стоп-вимоги
EXCLUDED = re.compile(r"yandex|яндекс|mail\.ru|\bvk\b|вк\b|russia|росі|belarus|білорус|casino|казино|betting|"
                      r"гембл|gambl|букмекер|adult|forex|форекс|binary option", re.I)
# Бази сайтів — єдине визначення для compare.py і monthly.py.
#   strict = вакансії, які пайплайн показав або на які подано;
#   wide   = strict + відсіяні за критеріями кандидата (роль/досвід/формат/ЗП/…, див. PERSONAL_CODES) і «майже підійшли».
# Технічні відсіви (дубль, помилка, ліміт) не входять нікуди. Telegram-сторона: strict = вакансії після profile_reject,
# wide = усі вакансії Telegram періоду (збіг із сайтами від профілю не залежить).
SITE_STRICT = ("shown", "applied")
SITE_WIDE = ("shown", "applied", "personal_reject")
MATCH_WINDOW_DAYS = 30
FUZZY_JACCARD = 0.6
# Коди відсіву за критеріями кандидата (реальна вакансія, не підійшла саме йому) — потрапляють у широку базу.
PERSONAL_CODES = {"роль", "досвід", "формат", "зп", "англійська", "військова служба"}

_SKILL_ALIASES = {"postgres": "postgresql", "gbq": "bigquery", "google bigquery": "bigquery",
                  "google data studio": "looker studio", "data studio": "looker studio", "powerbi": "power bi",
                  "ms excel": "excel", "microsoft excel": "excel", "gsheets": "google sheets",
                  "google таблиці": "google sheets", "a/b": "a/b testing", "ab testing": "a/b testing",
                  "a/b тести": "a/b testing", "a/b-тестування": "a/b testing"}
_TITLE_SYN = {"аналітик": "analyst", "analytics": "analyst", "analyst": "analyst", "даних": "data", "дані": "data",
              "розробник": "developer", "developer": "developer", "інженер": "engineer", "engineer": "engineer",
              "менеджер": "manager", "manager": "manager", "тестувальник": "qa", "tester": "qa"}
_TITLE_STOP = {"and", "for", "at", "of", "the", "і", "та", "в", "у", "з", "to"}
_LEGAL = re.compile(r"\b(тов|tov|ооо|ooo|llc|ltd|inc|gmbh|sp|zoo|group|company|the|corp|co)\b")
_TR = {"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ye", "ж": "zh", "з": "z",
       "и": "y", "і": "i", "ї": "yi", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
       "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "shch",
       "ю": "yu", "я": "ya", "ы": "y", "э": "e", "ь": "", "ъ": "", "'": "", "’": ""}
_DIR_GUESS = [("analytics", r"analyst|аналітик|\bbi\b|\bdata\b|sql|tableau|power bi|looker"),
              ("qa", r"\bqa\b|test|тестув"), ("dev", r"developer|engineer|розробник|програміст|backend|frontend"),
              ("pm", r"project manager|product manager|\bpm\b|scrum")]


# ----------------------------------------------------------------------------- ключі зіставлення
def company_key(name: str) -> str:
    s = "".join(_TR.get(ch, ch) for ch in (name or "").lower())
    s = _LEGAL.sub(" ", re.sub(r"[^\w\s]", " ", s))
    return re.sub(r"\s+", "", s)


def title_tokens(title: str) -> frozenset:
    toks = [_TITLE_SYN.get(t, t) for t in norm_title(title or "").split()]
    return frozenset(t for t in toks if t not in _TITLE_STOP)


def url_key(url: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", (url or "").strip().lower())
    return re.sub(r"[?#].*$", "", u).rstrip("/")


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def parse_date(text: str) -> date | None:
    text = text or ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    return date(int(m[3]), int(m[2]), int(m[1])) if m else None


def guess_direction(title: str) -> str:
    low = (title or "").lower()
    return next((d for d, pat in _DIR_GUESS if re.search(pat, low)), "other")


def skill_set(text: str) -> set[str]:
    out = (t.strip().lower() for t in (text or "").split(";"))
    return {_SKILL_ALIASES.get(t, t) for t in out if t}


# ----------------------------------------------------------------------------- сторона сайтів
_RANK = {"shown": 3, "applied": 2, "personal_reject": 1, "technical": 0}


def board_status(result: str, code: str) -> str | None:
    r, c = (result or "").strip().lower(), (code or "").strip().lower()
    if "підсумок" in r or r.startswith("не оцінено"):
        return None
    if r.startswith("майже"):          # блок «Майже підійшли»: не показано, не пройшов один критерій
        return "personal_reject"
    if "відсі" in r:
        if c == "подано":
            return "applied"
        return "personal_reject" if c in PERSONAL_CODES else "technical"
    return "shown"


def load_board(logs: Path, start: date, end: date) -> tuple[list[dict], Counter, int]:
    """Унікальні вакансії із сайтів за період. Telegram-джерела пайплайну відкидаємо (інакше порівняння циркулярне)."""
    raw, seen_results, tg_rows = [], Counter(), 0
    for path in sorted(logs.glob("*.csv")):
        if path.name in ("source_counts.csv", "sheet_rows.csv"):
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if "Результат" not in r:
                    break
                status = board_status(r.get("Результат"), r.get("Причина"))
                seen_results[(r.get("Результат", ""), r.get("Причина", ""))] += 1
                if status is None:
                    continue
                day = parse_date(r.get("Дата публікації")) or parse_date(r.get("Дата запуску (UTC)"))
                if not day or not (start <= day <= end):
                    continue
                if re.search(r"telegram|телеграм|\btg\b", r.get("Джерело", ""), re.I):
                    tg_rows += 1
                    continue
                label = r.get("Посада — компанія", "")
                title, _, company = label.rpartition(" — ") if " — " in label else (label, "", "")
                raw.append({"title": title.strip(), "company": company.strip(), "url": r.get("URL", ""),
                            "date": day, "status": status, "source": r.get("Джерело", "")})
    raw.sort(key=lambda r: r["date"])
    groups: dict[tuple, dict] = {}
    by_url: dict[str, dict] = {}
    for r in raw:
        r.update(title_key=norm_title(r["title"]), company_key=company_key(r["company"]),
                 url_key=url_key(r["url"]), tokens=title_tokens(r["title"]))
        g = by_url.get(r["url_key"]) if r["url_key"] else None
        if g is None:
            g = groups.get((r["title_key"], r["company_key"]))
            if g is not None and (r["date"] - g["date"]).days > MATCH_WINDOW_DAYS:
                g = None
        if g is None:
            g = dict(r)
            groups[(r["title_key"], r["company_key"])] = g
            if r["url_key"]:
                by_url[r["url_key"]] = g
        else:
            g["date"] = r["date"]
            if _RANK[r["status"]] > _RANK[g["status"]]:
                g["status"] = r["status"]
    uniques = list({id(g): g for g in groups.values()}.values())
    for g in uniques:
        g["direction"] = guess_direction(g["title"])
    return uniques, seen_results, tg_rows


# ----------------------------------------------------------------------------- сторона Telegram
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_tg(start: date, end: date) -> list[dict]:
    rows = load("vacancies.csv")
    uniques = dedup(rows)
    by_key = defaultdict(list)
    for r in rows:
        by_key[(r["_title"], r["_company"])].append(r)
    for u in uniques:
        reclassify(u)
        cluster = [r for r in by_key[(norm_title(u["title"]), norm_company(u["company"]))]
                   if u["first_seen"] <= r["datetime"][:10] <= u["last_seen"]] or []
        # .get: у старих vacancies.csv могло не бути частини колонок (salary_period, experience_years_min тощо)
        exps = [e for e in (_num(r.get("experience_years_min")) for r in cluster) if e is not None]
        sal = next((r for r in cluster if _num(r.get("salary_min")) is not None or _num(r.get("salary_max")) is not None), None)
        u.update(experience=min(exps) if exps else None,
                 salary_min=_num(sal.get("salary_min")) if sal else None, salary_max=_num(sal.get("salary_max")) if sal else None,
                 salary_currency=(sal.get("salary_currency") or "") if sal else "", salary_period=(sal.get("salary_period") or "") if sal else "")
    normalize_skills(uniques)
    for u in uniques:
        u.update(date=parse_date(u["first_seen"]), last=parse_date(u["last_seen"]), title_key=norm_title(u["title"]),
                 company_key=company_key(u["company"]), url_key=url_key(u["link"]), tokens=title_tokens(u["title"]),
                 skill_set=skill_set(u["skills"]), is_it_b=str(u["is_it"]) == "True")
    return [u for u in uniques if u["date"] and start <= u["date"] <= end]


def profile_reject(u: dict) -> str | None:
    """Перша причина відсіву за профілем (ті самі коди, що REJECT_CODES пайплайну) або None."""
    if not u["is_it_b"] or u["direction"] not in FIT_DIRECTIONS:
        return "роль"
    if u["level"] not in ACCEPT_LEVELS or (u["experience"] is not None and u["experience"] > MAX_EXPERIENCE_YEARS):
        return "досвід"
    if u["work_format"] not in ACCEPT_FORMATS:
        return "формат"
    floor = SALARY_FLOOR_MONTH.get((u["salary_currency"] or "").upper())
    low = u["salary_min"] if u["salary_min"] is not None else u["salary_max"]
    if floor and u["salary_period"] == "month" and low is not None and low < floor:
        return "ЗП"
    if EXCLUDED.search(f"{u['title']} {u['company']} {u['skills']}"):
        return "виключення"
    if u["skill_set"] & REJECT_SKILLS:
        return "стоп-навик"
    return None


# ----------------------------------------------------------------------------- зіставлення
def match_level(t: dict, b: dict) -> str | None:
    """url | exact | fuzzy | title_only. title_only (одна зі сторін без компанії) — лише верхня межа."""
    if t["url_key"] and t["url_key"] == b["url_key"]:
        return "url"
    lo, hi = t["date"] - timedelta(days=MATCH_WINDOW_DAYS), (t["last"] or t["date"]) + timedelta(days=MATCH_WINDOW_DAYS)
    if not lo <= b["date"] <= hi:
        return None
    same, jac = t["title_key"] == b["title_key"] or t["tokens"] == b["tokens"], jaccard(t["tokens"], b["tokens"])
    if t["company_key"] and b["company_key"]:
        if t["company_key"] == b["company_key"] or (min(len(t["company_key"]), len(b["company_key"])) >= 4 and
                                                    (t["company_key"].startswith(b["company_key"]) or
                                                     b["company_key"].startswith(t["company_key"]))):
            return "exact" if same else ("fuzzy" if jac >= FUZZY_JACCARD else None)
        return None
    return "title_only" if same else None


def best_matches(left: list[dict], right: list[dict], left_is_tg: bool = False) -> dict[int, tuple[int, str]]:
    """Для кожного елемента left (за індексом) — найкращий збіг у right. match_level(tg, board)."""
    order = {"url": 3, "exact": 2, "fuzzy": 1, "title_only": 0}
    out: dict[int, tuple[int, str]] = {}
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            lvl = match_level(a, b) if left_is_tg else match_level(b, a)
            if lvl and (i not in out or order[lvl] > order[out[i][1]]):
                out[i] = (j, lvl)
    return out


# ----------------------------------------------------------------------------- аудит збігів
_LEVEL_ORDER = {"url": 3, "exact": 2, "fuzzy": 1, "title_only": 0}
AUDIT_FIELDS = ["month", "level", "is_best", "counted_in", "site_status", "in_strict", "site_title", "site_company",
                "site_url", "site_date", "tg_title", "tg_company", "tg_link", "tg_first_seen", "tg_channels",
                "url_equal", "company_equal", "title_equal", "title_jaccard", "company_sim", "date_gap_days",
                "score", "verdict", "note"]
VERDICTS = {"y": True, "n": False}


def company_similarity(a: str, b: str) -> float | None:
    """1.0 — однакові або одна є початком іншої (≥4 знаків); інакше схожість ключів; None — компанії немає з боку."""
    if not a or not b:
        return None
    if a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a))):
        return 1.0
    return round(SequenceMatcher(None, a, b).ratio(), 2)


def pair_score(t: dict, b: dict) -> dict:
    """Описова оцінка пари (НЕ ймовірність і не впливає на рішення про збіг): 0,6·схожість назв + 0,4·схожість компаній.
    URL-збіг = 1.0. Потрібна, щоб при перевірці пар одразу бачити слабкі."""
    url_eq = bool(t["url_key"]) and t["url_key"] == b["url_key"]
    jac = round(jaccard(t["tokens"], b["tokens"]), 2)
    comp = company_similarity(t["company_key"], b["company_key"])
    score = 1.0 if url_eq else round(0.6 * jac + 0.4 * (comp or 0.0), 2)
    return {"url_equal": url_eq, "title_equal": t["title_key"] == b["title_key"] or t["tokens"] == b["tokens"],
            "title_jaccard": jac, "company_sim": "" if comp is None else comp,
            "company_equal": comp == 1.0, "date_gap_days": abs((b["date"] - t["date"]).days), "score": score}


def audit_pairs(board: list[dict], tg: list[dict], month: str, strict_ids: set[int] | None = None) -> list[dict]:
    """Усі пари «вакансія сайту ↔ вакансія Telegram», які пройшли match_level, з ключами й оцінками.
    is_best — пара, що реально рахується в покритті (та сама, яку обирає best_matches: найвищий рівень, при рівності
    перша). counted_in: low+high для url/exact/fuzzy; high_only для title_only (лише верхня межа діапазону)."""
    rows = []
    for b in board:
        cands = [(ti, t, lvl) for ti, t in enumerate(tg) if (lvl := match_level(t, b))]
        if not cands:
            continue
        best_ti = max(cands, key=lambda c: (_LEVEL_ORDER[c[2]], -c[0]))[0]
        for ti, t, lvl in cands:
            sc = pair_score(t, b)
            rows.append({
                "month": month, "level": lvl, "is_best": ti == best_ti,
                "counted_in": "low+high" if lvl != "title_only" else "high_only",
                "site_status": b["status"], "in_strict": id(b) in (strict_ids or set()),
                "site_title": b["title"], "site_company": b["company"], "site_url": b["url"], "site_date": b["date"],
                "tg_title": t["title"], "tg_company": t["company"], "tg_link": t["link"],
                "tg_first_seen": t["first_seen"], "tg_channels": t["channels"], **sc, "verdict": "", "note": ""})
    rows.sort(key=lambda r: (-_LEVEL_ORDER[r["level"]], not r["is_best"], r["score"]))
    return rows


def validate_verdicts(rows: list[dict]) -> list[str]:
    return [f"рядок {i}: verdict={r['verdict']!r} (треба y або n)"
            for i, r in enumerate(rows, start=2) if r.get("verdict", "").strip() and r["verdict"].strip().lower() not in VERDICTS]


def score_audit(rows: list[dict]) -> list[dict]:
    """Точність зіставлення за ручною розміткою: частка пар (is_best) з verdict=y по рівнях збігу, з 95% інтервалом Вілсона."""
    from tg_market.gold import wilson
    best = [r for r in rows if str(r.get("is_best")) == "True" and r.get("verdict", "").strip().lower() in VERDICTS]
    groups = {lvl: [r for r in best if r["level"] == lvl] for lvl in _LEVEL_ORDER}
    groups["low+high (url/exact/fuzzy)"] = [r for r in best if r["level"] != "title_only"]
    out = []
    for name, items in groups.items():
        n = len(items)
        k = sum(1 for r in items if VERDICTS[r["verdict"].strip().lower()])
        lo, hi = wilson(k, n)
        out.append({"level": name, "n_checked": n, "n_correct": k,
                    "precision_pct": round(100 * k / n, 1) if n else None,
                    "ci95_low_pct": round(100 * lo, 1) if n else None, "ci95_high_pct": round(100 * hi, 1) if n else None})
    return out


def rng(hit_low: int, hit_high: int, n: int) -> str:
    if not n:
        return "—"
    a, b = round(100 * hit_low / n, 1), round(100 * hit_high / n, 1)
    return f"{a}" if a == b else f"{a}–{b}"


def inv(hit_low: int, hit_high: int, n: int) -> str:
    """Додаток до 100%: верхня межа покриття дає нижню межу втрати."""
    if not n:
        return "—"
    a, b = round(100 - 100 * hit_high / n, 1), round(100 - 100 * hit_low / n, 1)
    return f"{a}" if a == b else f"{a}–{b}"


def write_csv(name: str, rows: list[dict]) -> None:
    if not rows:
        return
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def previous_month(today: date) -> str:
    first = today.replace(day=1)
    prev = first - timedelta(days=1)
    return f"{prev.year}-{prev.month:02d}"


# ----------------------------------------------------------------------------- головна логіка
def run(month: str, logs: Path) -> dict:
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)

    board, results, tg_pipeline_rows = load_board(logs, start, end)
    tg = load_tg(start, end)
    strict = [b for b in board if b["status"] in SITE_STRICT]
    wide = [b for b in board if b["status"] in SITE_WIDE]

    # покриття: чи є вакансія із сайтів серед УСІХ Telegram-вакансій періоду (профіль не впливає на збіг)
    def coverage(base):
        m = best_matches(base, tg)
        low = sum(1 for v in m.values() if v[1] in ("url", "exact", "fuzzy"))
        return m, low, len(m)

    m_strict, lo_s, hi_s = coverage(strict)
    m_wide, lo_w, hi_w = coverage(wide)

    funnel, fit = Counter(), []
    for u in tg:
        code = profile_reject(u)
        u["reject"] = code
        funnel[code or "пройшла"] += 1
        if code is None:
            fit.append(u)
    entry = [u for u in tg if u["is_it_b"] and u["direction"] in FIT_DIRECTIONS and u["level"] in ACCEPT_LEVELS
             and (u["experience"] is None or u["experience"] <= MAX_EXPERIENCE_YEARS)]
    tg_vs_board = best_matches(fit, wide, left_is_tg=True)       # TG-вакансії профілю, що знайшлися в сайтах (wide)
    excl_low = len(fit) - sum(1 for v in tg_vs_board.values() if v[1] in ("url", "exact", "fuzzy"))
    excl_high = len(fit) - len(tg_vs_board)

    note = lambda n: "мала вибірка (n<%d)" % SMALL_N if n < SMALL_N else ""
    summary = [
        {"метрика": "Унікальні вакансії на сайтах (показані+подані)", "значення": len(strict), "n": len(strict), "примітка": note(len(strict))},
        {"метрика": "Унікальні вакансії на сайтах (+ відсіяні за роль/досвід/формат/ЗП)", "значення": len(wide), "n": len(wide), "примітка": note(len(wide))},
        {"метрика": "Унікальні IT-вакансії в Telegram (усі напрями)", "значення": sum(1 for u in tg if u["is_it_b"]), "n": len(tg), "примітка": ""},
        {"метрика": "Telegram: напрям аналітика", "значення": sum(1 for u in tg if u["is_it_b"] and u["direction"] in FIT_DIRECTIONS), "n": len(tg), "примітка": ""},
        {"метрика": "Telegram: під мій профіль (після фільтрів)", "значення": len(fit), "n": len(tg), "примітка": note(len(fit))},
        {"метрика": "Покриття Telegram, % від сайтів (показані+подані) [низ–верх]", "значення": rng(lo_s, hi_s, len(strict)), "n": len(strict), "примітка": note(len(strict))},
        {"метрика": "ВТРАТА без сайтів, % (показані+подані) [низ–верх]", "значення": inv(lo_s, hi_s, len(strict)), "n": len(strict), "примітка": note(len(strict))},
        {"метрика": "Покриття Telegram, % від сайтів (wide) [низ–верх]", "значення": rng(lo_w, hi_w, len(wide)), "n": len(wide), "примітка": note(len(wide))},
        {"метрика": "ВТРАТА без сайтів, % (wide) [низ–верх]", "значення": inv(lo_w, hi_w, len(wide)), "n": len(wide), "примітка": note(len(wide))},
        {"метрика": "Ексклюзив Telegram (профіль), % — немає серед знайдених пайплайном [низ–верх]", "значення": (f"{round(100*excl_high/len(fit),1)}–{round(100*excl_low/len(fit),1)}" if fit else "—"), "n": len(fit), "примітка": "верхня межа ексклюзивності: сайти бачимо лише через пошукові запити пайплайну"},
    ]
    for key, label in (("level", "рівень не вказано"), ("work_format", "формат не вказано")):
        k = sum(1 for u in fit if u[key] == "unspecified")
        summary.append({"метрика": f"Telegram (профіль): {label}, %", "значення": round(100 * k / len(fit), 1) if fit else "—", "n": len(fit), "примітка": note(len(fit))})
    k = sum(1 for u in fit if u["salary_stated"])
    summary.append({"метрика": "Telegram (профіль): ЗП вказана, %", "значення": round(100 * k / len(fit), 1) if fit else "—", "n": len(fit), "примітка": note(len(fit))})

    by_dir = []
    for d in sorted({b["direction"] for b in strict}):
        base = [i for i, b in enumerate(strict) if b["direction"] == d]
        lo = sum(1 for i in base if i in m_strict and m_strict[i][1] in ("url", "exact", "fuzzy"))
        hi = sum(1 for i in base if i in m_strict)
        by_dir.append({"напрям (за назвою, евристика)": d, "n": len(base), "втрата_%": inv(lo, hi, len(base)), "примітка": note(len(base))})

    demand = Counter(s for u in entry for s in u["skill_set"])
    n_sk = sum(1 for u in entry if u["skill_set"])
    skills = [{"навик": s, "частка_вакансій_%": round(100 * c / n_sk, 1), "n_з_навиками": n_sk,
               "статус": "мій" if s in MY_SKILLS else ("вчу" if s in LEARNING_SKILLS else "прогалина")}
              for s, c in demand.most_common(30)] if n_sk else []

    write_csv("compare_summary.csv", summary)
    write_csv("compare_funnel.csv", [{"код": k, "вакансій": v} for k, v in funnel.most_common()])
    write_csv("compare_loss_by_direction.csv", by_dir)
    write_csv("compare_skills.csv", skills)
    write_csv("compare_missing_in_tg.csv", [{"посада": b["title"], "компанія": b["company"], "дата": b["date"], "джерело": b["source"], "url": b["url"]}
                                            for i, b in enumerate(strict) if i not in m_strict])
    write_csv("compare_tg_exclusive.csv", [{"посада": u["title"], "компанія": u["company"], "дата": u["first_seen"], "канали": u["channels"], "посилання": u["link"]}
                                           for i, u in enumerate(fit) if i not in tg_vs_board])
    audit = audit_pairs(wide, tg, month, {id(b) for b in strict})
    write_csv("compare_match_audit.csv", audit)
    write_csv("compare_matches.csv", [{"сайт": strict[i]["title"] + " — " + strict[i]["company"], "telegram": tg[j]["title"] + " — " + tg[j]["company"], "рівень": lvl}
                                      for i, (j, lvl) in sorted(m_strict.items())])
    return {"summary": summary, "funnel": funnel, "skills": skills, "by_dir": by_dir, "results": results,
            "tg_pipeline_rows": tg_pipeline_rows, "period": (start, end), "audit": audit}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default=previous_month(date.today()), help="YYYY-MM (за замовчуванням — попередній місяць)")
    ap.add_argument("--logs", default=str(Path(__file__).resolve().parent.parent / "logs"), help="каталог logs/ пайплайну")
    ap.add_argument("--score-audit", metavar="CSV", help="оцінити точність зіставлення за ручним verdict (y/n) у compare_match_audit.csv")
    args = ap.parse_args()
    if args.score_audit:
        with open(args.score_audit, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        errs = validate_verdicts(rows)
        if errs:
            sys.exit("Помилки в розмітці:\n  " + "\n  ".join(errs[:30]))
        res = score_audit(rows)
        for r in res:
            print(f"  {r['level']:<28} n={r['n_checked']:<4} правильних {r['n_correct']:<4} "
                  f"{r['precision_pct']}% [{r['ci95_low_pct']}–{r['ci95_high_pct']}]")
        write_csv("compare_match_precision.csv", res)
        return
    logs = Path(args.logs)
    if not logs.is_dir() or not (DATA / "vacancies.csv").exists():
        sys.exit(f"Потрібні: каталог логів ({logs}) і {DATA / 'vacancies.csv'} (спершу tg_market.collect)")
    res = run(args.month, logs)
    print(f"\nПеріод {res['period'][0]} … {res['period'][1]}")
    for r in res["summary"]:
        print(f"  {r['метрика']}: {r['значення']}  (n={r['n']}) {r['примітка']}")
    print("\nВоронка фільтрів профілю Telegram:", dict(res["funnel"]))
    gaps = [s for s in res["skills"] if s["статус"] == "прогалина"][:8]
    if gaps:
        print("Топ прогалин у навичках (аналітика, entry-рівень):", ", ".join(f"{s['навик']} {s['частка_вакансій_%']}%" for s in gaps))
    print(f"Рядків Telegram-джерел пайплайну (виключено з бази сайтів): {res['tg_pipeline_rows']}")
    print("Значення «Результат/Причина» у логах (перевір, що класифікація статусів коректна):")
    for (a, b), n in res["results"].most_common(12):
        print(f"  {a or '—'} / {b or '—'}: {n}")
    best = [r for r in res["audit"] if r["is_best"]]
    print(f"Аудит збігів: {len(best)} пар у покритті ({Counter(r['level'] for r in best).most_common()}); "
          f"перевір вибірково compare_match_audit.csv (стовпець verdict y/n), далі --score-audit")
    print(f"\nФайли: {OUT}/compare_*.csv")


if __name__ == "__main__":
    main()

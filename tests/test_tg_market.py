"""tg_market: розбір стрічки t.me/s і нормалізація полів вакансії (без мережі й API)."""
from pathlib import Path

from tg_market.classify import normalize_vacancy
from tg_market.parse import parse_page, parse_views

FIXTURE = Path(__file__).parent / "fixtures" / "tg_channel_page.html"


def test_parse_page_extracts_id_date_views_text():
    posts = parse_page(FIXTURE.read_text(encoding="utf-8"), "Job_IT_Junior")
    assert [p.post_id for p in posts] == [10561, 10562]
    assert posts[0].datetime == "2026-09-26T14:00:00+00:00"
    assert posts[0].views == 1700
    assert posts[1].views == 970
    assert "Power BI" in posts[0].text
    assert "https://bit.ly/abc" in posts[0].text          # посилання на вакансію зберігається для Claude
    assert "recruiter_name" not in posts[0].text.split("Посилання:")[-1]  # контакт t.me — ні


def test_parse_views_units():
    assert parse_views("1.7K") == 1700
    assert parse_views("2,3M") == 2_300_000
    assert parse_views("") is None


def test_normalize_vacancy_falls_back_to_allowed_values():
    v = normalize_vacancy({"title": " Data Analyst ", "is_it": True, "direction": "BI", "level": "Strong Junior",
                           "work_format": "Remote", "skills": ["SQL", " Python ", ""]})
    assert v["direction"] == "other-it"   # невідоме значення → запасне, а не сміття в даних
    assert v["level"] == "unspecified"
    assert v["work_format"] == "remote"
    assert v["skills"] == "SQL; Python"


def _m(ch, day, title, company="", direction="dev", is_it="True", level="junior", skills=""):
    return {"channel": ch, "post_id": "1", "datetime": f"2026-08-{day:02d}T10:00:00+00:00", "vacancy_idx": "0",
            "title": title, "company": company, "is_it": is_it, "direction": direction, "level": level,
            "experience_years_min": "", "skills": skills, "work_format": "remote", "remote_scope": "",
            "salary_stated": "False", "salary_min": "", "salary_max": "", "salary_currency": "",
            "salary_period": "", "link": ""}


def test_dedup_merges_reposts_and_fills_company():
    from tg_market.analyze import dedup
    rows = [
        _m("a", 1, "Embedded Hardware Engineer", "Skif", direction="dev"),
        _m("a", 8, "Embedded Hardware Engineer", "", direction="other-it"),   # репост без компанії
        _m("b", 9, "Embedded Hardware Engineer (STM32)", "skif", direction="dev"),  # інший канал
        _m("a", 1, "Junior Python Developer", "X"),
    ]
    u = dedup(rows)
    emb = [x for x in u if "embedded" in x["title"].lower()]
    assert len(u) == 2 and len(emb) == 1
    assert emb[0]["mentions"] == 3 and emb[0]["channels"] == "a; b"
    assert emb[0]["direction"] == "dev"          # мода, а не перша згадка


def test_dedup_splits_after_window():
    from tg_market.analyze import dedup
    rows = [_m("a", 1, "QA Engineer", "Y"), {**_m("a", 1, "QA Engineer", "Y"), "datetime": "2026-09-20T10:00:00+00:00"}]
    assert len(dedup(rows)) == 2                  # розрив 50 днів > 30 — нова вакансія


def test_shares_include_n_and_small_sample_flag():
    from tg_market.analyze import shares
    rows = [{"d": "qa", "f": "remote"}, {"d": "qa", "f": "office"}, {"d": "qa", "f": "remote"}]
    out = shares(rows, lambda r: r["d"], lambda r: r["f"])
    remote = next(r for r in out if r["value"] == "remote")
    assert remote["share_pct"] == 66.7 and remote["n"] == 3 and remote["small_sample"] is True


def test_classify_splits_chunk_on_truncated_json(monkeypatch):
    import tg_market.collect as col
    from claude_orchestrator.client import ClaudeCallError
    from tg_market.parse import Post

    posts = [Post("c", i, "2026-09-01T00:00:00+00:00", 1, f"t{i}") for i in range(4)]
    calls = []

    def fake(prompt, max_tokens):
        n = prompt.count('"key"')
        calls.append(n)
        if n > 2:
            raise ClaudeCallError("обірвано")
        if '"c/3"' in prompt and n == 1:
            raise ClaudeCallError("один поганий пост")
        keys = [f"c/{i}" for i in range(4) if f'"c/{i}"' in prompt]
        return {"posts": [{"key": k, "is_vacancy": False, "vacancies": []} for k in keys]}

    monkeypatch.setattr(col, "call_json", fake)
    res = col.classify(posts)
    assert set(res) >= {"c/0", "c/1", "c/2"}      # решта розібрана попри збої


def test_reclassify_creative_design_and_ngo_analyst():
    from tg_market.analyze import reclassify
    g = {"title": "Графічний дизайнер/графічна дизайнерка", "direction": "design", "is_it": "True"}
    assert reclassify(g) == "creative-design→marketing" and g["is_it"] == "False"
    ux = {"title": "Brand & UI/UX Designer", "direction": "design", "is_it": "True"}
    assert reclassify(ux) is None and ux["is_it"] == "True"
    meal = {"title": "MEAL-менеджер", "direction": "analytics", "is_it": "True"}
    assert reclassify(meal) == "non-data-analyst→non-it"
    da = {"title": "Junior Data Analyst", "direction": "analytics", "is_it": "True"}
    assert reclassify(da) is None


def test_normalize_skills_and_full_weeks():
    from tg_market.analyze import normalize_skills, full_weeks
    us = [{"skills": "Git; SQL"}, {"skills": "git; Git"}, {"skills": ""}]
    normalize_skills(us)
    assert us[0]["skills"] == "Git; SQL" and us[1]["skills"] == "Git"
    rows = [{"first_seen": d} for d in ("2026-08-01", "2026-08-03", "2026-08-09", "2026-08-10")]
    # 01.08 (субота) — неповний тиждень; 10.08 — понеділок останнього неповного тижня
    assert [r["first_seen"] for r in full_weeks(rows)] == ["2026-08-03", "2026-08-09"]

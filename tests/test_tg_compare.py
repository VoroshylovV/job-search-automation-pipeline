"""tg_market.compare: ключі зіставлення, статуси логу, профільні фільтри (без мережі й API)."""
from datetime import date

from tg_market.compare import board_status, company_key, jaccard, match_level, previous_month, profile_reject, title_tokens, url_key


def _tg(title="Junior Data Analyst", company="Ornis", **kw):
    from tg_market.analyze import norm_title
    base = dict(title=title, company=company, date=date(2026, 10, 3), last=date(2026, 10, 3), title_key=norm_title(title),
                company_key=company_key(company), url_key="", tokens=title_tokens(title), skill_set=set(), skills="",
                is_it_b=True, direction="analytics", level="junior", experience=None, work_format="remote",
                salary_min=None, salary_max=None, salary_currency="", salary_period="", salary_stated=False)
    base.update(kw)
    return base


def test_company_key_translit_and_legal_forms():
    assert company_key("ТОВ «Є гроші»") == company_key("Ye Hroshi LLC")
    assert company_key("Ornis") == company_key("ORNIS")


def test_url_key_strips_scheme_query_and_slash():
    assert url_key("https://www.DOU.ua/jobs/1/?utm=x#a") == "dou.ua/jobs/1"


def test_title_synonyms_match_ukrainian_and_english():
    assert jaccard(title_tokens("Аналітик даних"), title_tokens("Data Analyst")) == 1.0


def test_board_status_codes():
    assert board_status("показано", "") == "shown"
    assert board_status("відсіяно", "подано") == "applied"
    assert board_status("відсіяно", "ЗП") == "personal_reject"
    assert board_status("відсіяно", "дубль") == "technical"
    assert board_status("ПІДСУМОК", "") is None


def test_board_status_near_miss_and_unevaluated_from_real_logs():
    # «майже» — блок «Майже підійшли»: НЕ показано, не відповідає одному критерію → лише широка база
    assert board_status("майже", "досвід") == "personal_reject"
    assert board_status("відсіяно", "військова служба") == "personal_reject"
    assert board_status("не оцінено (ліміт етапу 2)", "ліміт_етапу_2") is None
    assert board_status("не оцінено (помилка Claude)", "помилка_Claude") is None


def test_match_levels_and_window():
    t, b = _tg(), _tg(title="Data Analyst (junior)", company="ORNIS", date=date(2026, 10, 10))
    assert match_level(t, b) == "exact"
    assert match_level(t, _tg(company="Other")) is None
    assert match_level(t, _tg(date=date(2026, 12, 20))) is None            # поза вікном 30 днів
    assert match_level(_tg(company=""), _tg(company="")) == "title_only"     # без компанії — лише верхня межа
    assert match_level({**t, "url_key": "x.io/1"}, {**b, "company": "Z", "company_key": "z", "url_key": "x.io/1"}) == "url"


def test_profile_reject_codes_follow_pipeline_criteria():
    assert profile_reject(_tg()) is None
    assert profile_reject(_tg(direction="qa")) == "роль"
    assert profile_reject(_tg(level="middle")) == "досвід"
    assert profile_reject(_tg(experience=3.0)) == "досвід"
    assert profile_reject(_tg(work_format="office")) == "формат"
    assert profile_reject(_tg(salary_min=20000.0, salary_currency="UAH", salary_period="month")) == "ЗП"
    assert profile_reject(_tg(salary_min=500.0, salary_currency="USD", salary_period="hour")) is None   # не місячна — не порівнюємо
    assert profile_reject(_tg(company="Yandex")) == "виключення"
    assert profile_reject(_tg(skill_set={"power bi", "dax"})) == "стоп-навик"


def test_previous_month_wraps_year():
    assert previous_month(date(2026, 11, 5)) == "2026-10"
    assert previous_month(date(2027, 1, 5)) == "2026-12"


# ----------------------------------------------------------------------------- аудит збігів
def _tgv(title, company, link="", day=3, **kw):
    return _tg(title=title, company=company, link=link, url_key=kw.pop("url_key", ""), first_seen=f"2026-10-{day:02d}",
               channels="ch", date=date(2026, 10, day), last=date(2026, 10, day), **kw)


def _site(title, company, status="shown", url="", day=5, url_key=""):
    base = _tg(title=title, company=company, date=date(2026, 10, day), last=date(2026, 10, day), url_key=url_key)
    base.update(status=status, url=url, source="djinni")
    return base


def test_audit_pairs_are_consistent_with_best_matches_and_carry_keys():
    from tg_market.compare import audit_pairs, best_matches
    tg = [_tgv("Data Analyst", "Ornis", link="https://x.io/a"), _tgv("Junior Data Analyst", "Ornis"),
          _tgv("QA Engineer", "")]
    board = [_site("Junior Data Analyst", "ORNIS"), _site("QA Engineer", ""), _site("Chef", "Foo")]
    rows = audit_pairs(board, tg, "2026-10", {id(board[0])})
    best = best_matches(board, tg)
    best_rows = [r for r in rows if r["is_best"]]
    assert len(best_rows) == len(best) == 2                                   # «Chef» не має жодного збігу — рядка немає
    by_title = {r["site_title"]: r for r in best_rows}
    assert by_title["Junior Data Analyst"]["tg_title"] == tg[best[0][0]]["title"]
    assert by_title["Junior Data Analyst"]["level"] == "exact" and by_title["Junior Data Analyst"]["in_strict"] is True
    assert by_title["QA Engineer"]["level"] == "title_only" and by_title["QA Engineer"]["counted_in"] == "high_only"
    assert by_title["QA Engineer"]["company_sim"] == ""                      # компанії немає — оцінка не вигадується
    assert all(r["verdict"] == "" for r in rows) and set(rows[0]) == set(__import__("tg_market.compare", fromlist=["x"]).AUDIT_FIELDS)


def test_audit_scores_url_match_highest_and_flags_weak_fuzzy():
    from tg_market.compare import audit_pairs
    url = _tgv("Analyst", "A", url_key="x.io/1")
    site_url = _site("Something else", "Z", url_key="x.io/1")
    fuzzy_tg = _tgv("Data Analyst Python", "Ornis")
    site_fz = _site("Data Analyst SQL Python", "ORNIS")
    rows = audit_pairs([site_url, site_fz], [url, fuzzy_tg], "2026-10")
    by = {r["site_title"]: r for r in rows}
    assert by["Something else"]["level"] == "url" and by["Something else"]["score"] == 1.0
    assert by["Data Analyst SQL Python"]["level"] == "fuzzy" and by["Data Analyst SQL Python"]["score"] < 1.0


def test_score_audit_precision_by_level_with_interval_and_validation():
    from tg_market.compare import score_audit, validate_verdicts
    rows = [{"level": "exact", "is_best": "True", "verdict": v} for v in ["y"] * 9 + ["n"]]
    rows += [{"level": "title_only", "is_best": "True", "verdict": v} for v in ["y", "n", "n", "n"]]
    rows += [{"level": "exact", "is_best": "False", "verdict": "n"}, {"level": "fuzzy", "is_best": "True", "verdict": ""}]
    res = {r["level"]: r for r in score_audit(rows)}
    assert (res["exact"]["n_checked"], res["exact"]["n_correct"], res["exact"]["precision_pct"]) == (10, 9, 90.0)
    assert res["exact"]["ci95_low_pct"] < 90 < res["exact"]["ci95_high_pct"]
    assert res["title_only"]["precision_pct"] == 25.0 and res["fuzzy"]["n_checked"] == 0   # не-best і порожні — не рахуються
    assert res["low+high (url/exact/fuzzy)"]["n_checked"] == 10
    assert validate_verdicts([{"verdict": "так"}, {"verdict": "Y"}, {"verdict": ""}]) == ["рядок 2: verdict='так' (треба y або n)"]


def test_wide_definition_is_shared_between_compare_and_monthly():
    from tg_market import compare, monthly
    assert compare.SITE_WIDE == monthly.WIDE == ("shown", "applied", "personal_reject")
    assert compare.SITE_STRICT == ("shown", "applied")

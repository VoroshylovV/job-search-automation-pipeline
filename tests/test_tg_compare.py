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

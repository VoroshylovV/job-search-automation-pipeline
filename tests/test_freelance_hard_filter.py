"""Жорсткий відсів Freelancehunt до оцінки Claude (Правило К, 18.10.2026)."""
from models import RawFreelanceProject
from pipeline.step1_2_freelance import _budget_uah, _hard_reject_reason


def _fh(title, description="", budget="", bids=10):
    return RawFreelanceProject(
        source="freelancehunt.com", title=title, url="https://freelancehunt.com/project/x/1.html",
        category="Базы данных и SQL", posted_raw="", bids_count=bids, budget_raw=budget,
        description_snippet=description,
    )


def test_budget_parsing():
    assert _budget_uah("") is None
    assert _budget_uah("не вказано") is None
    assert _budget_uah("5000 UAH") == 5000
    assert _budget_uah("200 USD") == 200 * 41.0


def test_specific_system_rejected_regardless_of_budget():
    assert _hard_reject_reason(_fh("Звіт у BAS Бухгалтерія", budget="20000 UAH"))
    assert _hard_reject_reason(_fh("Доробити обробку", "конфігурація 1С 8.3", budget="20000 UAH"))


def test_example_1658459_erp_planning_small_or_no_budget():
    # 29 ставок (середня конкуренція), замовник очікує 5–10 тис. грн за 2–4 дні.
    p = _fh("Автоматичне планування виробництва в ERP", "очікую 5–10 тис. грн, 2–4 дні", budget="", bids=29)
    assert _hard_reject_reason(p).startswith("ERP")
    assert _hard_reject_reason(_fh("ERP-звіт", budget="8000 UAH"))
    assert _hard_reject_reason(_fh("ERP-звіт", budget="30000 UAH")) is None


def test_no_budget_high_competition_rejected_medium_kept():
    assert _hard_reject_reason(_fh("SQL-запити для звіту", bids=41)) == "бюджет не вказано, висока конкуренція"
    assert _hard_reject_reason(_fh("SQL-запити для звіту", bids=29)) is None
    assert _hard_reject_reason(_fh("SQL-запити для звіту", budget="3000 UAH", bids=60)) is None


def test_regular_project_and_telegram_untouched():
    assert _hard_reject_reason(_fh("Дашборд у Looker Studio", budget="4000 UAH")) is None
    tg = RawFreelanceProject(source="telegram", title="1C developer needed", url="u", category="Telegram", posted_raw="")
    assert _hard_reject_reason(tg) is None


def test_html_card_budget_parsed_for_rule_k():
    from bs4 import BeautifulSoup
    from scrapers.freelancehunt import _parse_card
    html = ('<div><a href="/projects/erp-plan/1658459.html">Автоматичне планування в ERP</a>'
            '<span>29 ставок</span><span>8 000 UAH</span></div>')
    p = _parse_card(BeautifulSoup(html, "html.parser").div, "Базы данных и SQL")
    assert p.budget_raw == "8000 UAH" and p.bids_count == 29
    assert _hard_reject_reason(p).startswith("ERP")

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

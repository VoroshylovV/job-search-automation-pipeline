"""Етап 1 повертає стислий JSON (18.10.2026): лише поля для відсіву і ранжування."""
from datetime import date

from claude_orchestrator.prompts import build_vacancy_eval_parts
from models import RawJobPosting

JOB = RawJobPosting(source="djinni.co", title="Data Analyst", company="X", url="https://djinni.co/jobs/1-x/",
                    posted_raw="", salary_raw="", description_snippet="remote")


def test_card_stage_asks_for_compact_output():
    static, _ = build_vacancy_eval_parts([JOB], today=date(2026, 10, 19), stage="card")
    assert "СТИСЛИЙ ВИВІД НА ЕТАПІ 1" in static and "до 60 символів" in static


def test_full_stage_keeps_full_schema():
    static, _ = build_vacancy_eval_parts([JOB], today=date(2026, 10, 19), stage="full")
    assert "СТИСЛИЙ ВИВІД" not in static and '"match_reasoning"' in static

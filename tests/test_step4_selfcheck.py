"""Тести для pipeline/step4_selfcheck.py — деривація статусів
OK/ЧАСТКОВО/НЕ ВИКОНАНО з фактів Кроку 1 і Кроку 1.2. Чисті функції,
нічого мокати не треба."""
from __future__ import annotations

from models import SourceStatus
from pipeline.step4_selfcheck import _step1_2_status, _step1_status

ALL_VACANCY_SOURCES = ["djinni.co", "jobs.dou.ua", "robota.ua", "work.ua", "happymonday.ua"]


def test_step1_status_ok_when_all_sources_ok_and_dedup_level1():
    result = {
        "source_statuses": {s: SourceStatus(source=s, status="OK") for s in ALL_VACANCY_SOURCES},
        "dedup_level_used": 1,
    }
    status, note = _step1_status(result)
    assert status == "OK"


def test_step1_status_ne_vykonano_when_zero_sources_ok():
    result = {"source_statuses": {}, "dedup_level_used": 0}
    status, note = _step1_status(result)
    assert status == "НЕ ВИКОНАНО"


def test_step1_status_chastkovo_when_one_source_down():
    statuses = {s: SourceStatus(source=s, status="OK") for s in ALL_VACANCY_SOURCES}
    statuses["jobs.dou.ua"] = SourceStatus(source="jobs.dou.ua", status="недоступне")
    result = {"source_statuses": statuses, "dedup_level_used": 1}

    status, note = _step1_status(result)
    assert status == "ЧАСТКОВО"
    assert "4/5" in note


def test_step1_status_chastkovo_when_dedup_level2():
    result = {
        "source_statuses": {s: SourceStatus(source=s, status="OK") for s in ALL_VACANCY_SOURCES},
        "dedup_level_used": 2,
    }
    status, note = _step1_status(result)
    assert status == "ЧАСТКОВО"
    assert "рівень 2" in note


def test_step1_2_status_ok():
    result = {
        "source_statuses": {
            "freelancehunt.com": SourceStatus(source="freelancehunt.com", status="OK"),
            "telegram": SourceStatus(source="telegram", status="OK"),
        },
        "dedup_log_updated": True,
        "dedup_log_note": "",
        "metrics_saved": True,
    }
    status, note = _step1_2_status(result)
    assert status == "OK"
    assert note == ""


def test_step1_2_status_ne_vykonano_when_nothing_worked():
    result = {
        "source_statuses": {},
        "dedup_log_updated": False,
        "dedup_log_note": "",
        "metrics_saved": False,
        "metrics_error": "boom",
    }
    status, note = _step1_2_status(result)
    assert status == "НЕ ВИКОНАНО"
    assert "boom" in note


def test_step1_2_status_chastkovo_when_metrics_failed_but_sources_ok():
    result = {
        "source_statuses": {
            "freelancehunt.com": SourceStatus(source="freelancehunt.com", status="OK"),
            "telegram": SourceStatus(source="telegram", status="OK"),
        },
        "dedup_log_updated": True,
        "dedup_log_note": "",
        "metrics_saved": False,
        "metrics_error": "Sheets API timeout",
    }
    status, note = _step1_2_status(result)
    assert status == "ЧАСТКОВО"
    assert "Sheets API timeout" in note

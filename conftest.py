"""Гарантує, що корінь репозиторію є на sys.path незалежно від того, як
запущено pytest (bare `pytest`, `python -m pytest`, з іншої робочої
директорії тощо) — тести у tests/ імпортують `pipeline.*`, `models`,
`config`, `claude_orchestrator.*` як топрівневі модулі/пакети репозиторію,
а не як встановлений пакет."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


import pytest


@pytest.fixture(autouse=True)
def _no_real_tracker_read(monkeypatch):
    """Крок 1 при робочому лозі додатково читає URL таблиці відгуків
    (_applied_urls) — у тестах це мав би бути реальний Google API. Порожня
    множина за замовчуванням; окремі тести перевизначають її явно."""
    from pipeline import step1_vacancies

    monkeypatch.setattr(step1_vacancies, "_applied_urls", lambda: set())

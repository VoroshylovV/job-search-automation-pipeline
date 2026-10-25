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
    (_read_tracker_rows) — у тестах це мав би бути реальний Google API. Порожня
    множина за замовчуванням; окремі тести перевизначають її явно."""
    from pipeline import step1_vacancies

    monkeypatch.setattr(step1_vacancies, "_read_tracker_rows", lambda: [])


@pytest.fixture(autouse=True)
def _transparent_stage2(monkeypatch):
    """Етап 2 (повний текст) ходить у мережу. У тестах логіки Кроку 1 він
    «прозорий»: повертає оцінки етапу 1 без змін. Тести самого етапу 2
    (tests/test_step1_fulltext.py) знімають цю підміну явно."""
    from pipeline import step1_vacancies

    def _passthrough(jobs, today, stage1_evals=None, texts_out=None):
        return {k: ev for k, ev in enumerate(stage1_evals or [])}, set()

    monkeypatch.setattr(step1_vacancies, "_confirm_with_full_text", _passthrough)


@pytest.fixture(autouse=True)
def _no_shadow_by_default(monkeypatch):
    """Тіньова гілка етапу 1 (Haiku, з 26.10.2026) подвоїла б виклики call_json у
    тестах логіки Кроку 1, що лічать виклики/промпти. За замовчуванням вона вимкнена;
    тести тіні (tests/test_shadow_stage1.py) вмикають її явно."""
    from pipeline import step1_vacancies

    monkeypatch.setattr(step1_vacancies, "CLAUDE_MODEL_CARDS_SHADOW", "")

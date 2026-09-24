"""Гарантує, що корінь репозиторію є на sys.path незалежно від того, як
запущено pytest (bare `pytest`, `python -m pytest`, з іншої робочої
директорії тощо) — тести у tests/ імпортують `pipeline.*`, `models`,
`config`, `claude_orchestrator.*` як топрівневі модулі/пакети репозиторію,
а не як встановлений пакет."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

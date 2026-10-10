"""Облік вартості Claude за запуск: рахується з `usage` кожної відповіді API
(вхід, вихід, запис у кеш, читання з кешу) за цінами з config.py і
розкладається по етапах. Використовується клієнтом (client.call_json),
самоперевіркою (Крок 4), метриками (Крок 3) і журналом logs/costs.csv."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from config import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    FALLBACK_PRICE_PER_MTOK,
    MODEL_PRICES_PER_MTOK,
)

logger = logging.getLogger(__name__)

# Етапи обліку: cards — етап 1 (картки), fulltext — етап 2 (повні тексти),
# freelance — Крок 1.2, mail — Крок 2, other — все інше.
STAGES = ("cards", "fulltext", "freelance", "mail")
STAGE_TITLES = {
    "cards": "етап 1 (картки)",
    "fulltext": "етап 2 (повні тексти)",
    "freelance": "Крок 1.2 (фріланс)",
    "mail": "Крок 2 (пошта)",
    "other": "інше",
}


def price_for(model: str) -> tuple[float, float]:
    name = (model or "").lower()
    for family, price in MODEL_PRICES_PER_MTOK.items():
        if family in name:
            return price
    logger.warning("Невідома модель %r для підрахунку вартості — рахую за найдорожчою родиною", model)
    return FALLBACK_PRICE_PER_MTOK


def _tokens(usage, name: str) -> int:
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
    return int(value or 0)


def usage_cost(model: str, usage) -> float:
    """Вартість однієї відповіді в USD за її `usage` (об'єкт SDK або dict)."""
    p_in, p_out = price_for(model)
    return (
        _tokens(usage, "input_tokens") * p_in
        + _tokens(usage, "output_tokens") * p_out
        + _tokens(usage, "cache_creation_input_tokens") * p_in * CACHE_WRITE_MULTIPLIER
        + _tokens(usage, "cache_read_input_tokens") * p_in * CACHE_READ_MULTIPLIER
    ) / 1_000_000


@dataclass
class StageCost:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    models: set[str] = field(default_factory=set)


@dataclass
class CostTracker:
    stages: dict[str, StageCost] = field(default_factory=dict)

    def record(self, model: str, usage, stage: str = "other") -> float:
        cost = usage_cost(model, usage) if usage is not None else 0.0
        st = self.stages.setdefault(stage, StageCost())
        st.calls += 1
        st.usd += cost
        st.models.add(model)
        if usage is not None:
            st.input_tokens += _tokens(usage, "input_tokens")
            st.output_tokens += _tokens(usage, "output_tokens")
            st.cache_read_tokens += _tokens(usage, "cache_read_input_tokens")
            st.cache_write_tokens += _tokens(usage, "cache_creation_input_tokens")
        logger.info("Claude [%s] %s: $%.4f (разом за запуск $%.4f)", stage, model, cost, self.total_usd)
        return cost

    def stage(self, name: str) -> StageCost:
        return self.stages.get(name, StageCost())

    @property
    def total_usd(self) -> float:
        return sum(st.usd for st in self.stages.values())

    @property
    def calls(self) -> int:
        return sum(st.calls for st in self.stages.values())

    def reset(self) -> None:
        self.stages = {}


tracker = CostTracker()

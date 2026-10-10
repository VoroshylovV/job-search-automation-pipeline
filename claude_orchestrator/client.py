"""Тонка обгортка над anthropic SDK: викликає Claude, парсить JSON-відповідь,
ретраїть один раз при помилці парсингу (просить модель повернути ЛИШЕ JSON)."""
from __future__ import annotations

import json
import logging

import anthropic

from claude_orchestrator.cost import tracker
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)


class ClaudeCallError(Exception):
    pass


class ClaudeBudgetExceededError(ClaudeCallError):
    """Ліміт вартості запуску вичерпано — виклик не робиться."""


class ClaudeTruncatedError(ClaudeCallError):
    """Відповідь обрізана (stop_reason == "max_tokens"). Повторювати той самий
    запит марно — викликач має поділити батч і повторити менші частини."""


def _client() -> anthropic.Anthropic:
    if not ANTHROPIC_API_KEY:
        raise ClaudeCallError(
            "ANTHROPIC_API_KEY не задано. Додай його у .env (див. .env.example)."
        )
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def call_json(prompt: str, *, max_tokens: int = 8000, retries: int = 1, stage: str = "other",
              model: str | None = None, cache_prefix: str | None = None,
              budget_limit: float | None = None) -> dict:
    """Викликає Claude з prompt, очікує JSON-відповідь, повертає dict.

    stage — етап обліку вартості (cards | fulltext | freelance | mail | other),
    див. claude_orchestrator/cost.py. model — модель виклику (за замовчуванням
    config.CLAUDE_MODEL).

    cache_prefix — статична частина промпту (профіль + правила), що йде
    першим блоком з cache_control: наступні виклики з тим самим префіксом
    читають її з кешу (дешевше). prompt — динамічна частина. Кеш діє лише від
    мінімального розміру префікса моделі (≈1024+ токенів, для Haiku — більше);
    коротші префікси просто не кешуються, без помилки.

    budget_limit — якщо вартість запуску (cost.tracker) уже сягнула ліміту,
    виклик не робиться: ClaudeBudgetExceededError.

    При невалідному JSON — один повторний виклик з жорсткішою вимогою
    ("поверни ЛИШЕ JSON, без жодного тексту навколо").
    """
    client = _client()
    model = model or CLAUDE_MODEL
    current_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        if tracker.exceeded(budget_limit):
            raise ClaudeBudgetExceededError(
                f"ліміт бюджету запуску вичерпано (${tracker.total_usd:.4f} ≥ ${budget_limit:.2f})"
            )
        if cache_prefix:
            content = [
                {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": current_prompt},
            ]
        else:
            content = current_prompt
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        tracker.record(model, getattr(message, "usage", None), stage)
        if getattr(message, "stop_reason", None) == "max_tokens":
            # Обрізаний JSON не парситься, а повтор того самого запиту дасть
            # те саме — тому окрема помилка, а не "невалідний JSON".
            raise ClaudeTruncatedError(
                f"відповідь Claude обрізана за max_tokens={max_tokens}"
            )
        raw_text = "".join(
            block.text for block in message.content if block.type == "text"
        ).strip()

        # Claude інколи обгортає JSON у ```json ... ``` попри інструкцію —
        # знімаємо обгортку, якщо вона є.
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            last_error = exc
            logger.warning("Claude повернув невалідний JSON (спроба %d): %s", attempt + 1, exc)
            current_prompt = (
                prompt
                + "\n\nТВОЯ ПОПЕРЕДНЯ ВІДПОВІДЬ БУЛА НЕВАЛІДНИМ JSON. "
                "Поверни ЛИШЕ валідний JSON-об'єкт, без markdown-обгортки, "
                "без жодного тексту до чи після нього."
            )

    raise ClaudeCallError(f"Claude не повернув валідний JSON після {retries + 1} спроб: {last_error}")

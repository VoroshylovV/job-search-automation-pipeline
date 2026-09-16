"""Тонка обгортка над anthropic SDK: викликає Claude, парсить JSON-відповідь,
ретраїть один раз при помилці парсингу (просить модель повернути ЛИШЕ JSON)."""
from __future__ import annotations

import json
import logging

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)


class ClaudeCallError(Exception):
    pass


def _client() -> anthropic.Anthropic:
    if not ANTHROPIC_API_KEY:
        raise ClaudeCallError(
            "ANTHROPIC_API_KEY не задано. Додай його у .env (див. .env.example)."
        )
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def call_json(prompt: str, *, max_tokens: int = 8000, retries: int = 1) -> dict:
    """Викликає Claude з prompt, очікує JSON-відповідь, повертає dict.

    При невалідному JSON — один повторний виклик з жорсткішою вимогою
    ("поверни ЛИШЕ JSON, без жодного тексту навколо").
    """
    client = _client()
    current_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": current_prompt}],
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

"""Push-сповіщення наприкінці кожного запуску — безумовно, незалежно від
того, чи були знайдені нові вакансії й чи вдалися Кроки 1-4 (та сама логіка,
що була в чат-версії промпту: Володимир хоче знати, коли звіт готовий, а не
лише коли є щось "цікаве").

Чат-версія слала push через нативний інструмент телефону користувача —
самостійний Python-скрипт такого не має. Тут — мінімальна, безкоштовна і не
залежна від Google-акаунту заміна через ntfy.sh (публічний pub/sub без
реєстрації: POST на https://ntfy.sh/<topic>, підписка в мобільному застосунку
ntfy на ту саму тему). Якщо PUSH_NTFY_TOPIC не задано — сповіщення
пропускається з попередженням у лог; це НЕ зупиняє й не провалює запуск,
так само як недоступність будь-якого іншого допоміжного файлу.
"""
from __future__ import annotations

import logging

import requests

from config import PUSH_MESSAGE_TEXT, PUSH_NTFY_SERVER, PUSH_NTFY_TOPIC

logger = logging.getLogger(__name__)


def send_push(message: str = PUSH_MESSAGE_TEXT) -> bool:
    if not PUSH_NTFY_TOPIC:
        logger.warning(
            "PUSH_NTFY_TOPIC не задано в .env — push-сповіщення пропущено "
            "(звіт усе одно збережено в reports/ і виведено в stdout). "
            "Див. README, розділ 'Push-сповіщення'."
        )
        return False
    url = f"{PUSH_NTFY_SERVER.rstrip('/')}/{PUSH_NTFY_TOPIC}"
    try:
        resp = requests.post(url, data=message.encode("utf-8"), timeout=10)
        resp.raise_for_status()
        logger.info("Push-сповіщення надіслано (тема ntfy: %s)", PUSH_NTFY_TOPIC)
        return True
    except requests.RequestException as exc:
        logger.warning("Не вдалось надіслати push-сповіщення: %s", exc)
        return False

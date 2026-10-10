"""Gmail API: пошук листів за трьома незалежними умовами (ключові слова /
hr@-домени / компанії зі списку), керування лейблом, читання тіла листа."""
from __future__ import annotations

import base64
import json
import logging
import time

from googleapiclient.errors import HttpError

from config import (
    GMAIL_KEYWORDS,
    GMAIL_LABEL_NAME,
    GMAIL_RETRY_ATTEMPTS,
    GMAIL_RETRY_BASE_DELAY_SEC,
    GMAIL_SEARCH_WINDOW_DAYS,
)
from google_services.auth import gmail_service

logger = logging.getLogger(__name__)

_RATE_LIMIT_REASONS = {"ratelimitexceeded", "userratelimitexceeded"}


def _is_retryable(exc: HttpError) -> bool:
    """429 завжди; 403 — лише коли причина rateLimitExceeded/userRateLimitExceeded
    (інші 403, напр. insufficientPermissions, ретраїти марно)."""
    status = getattr(exc.resp, "status", None)
    if status == 429:
        return True
    if status != 403:
        return False
    try:
        errors = json.loads(exc.content.decode("utf-8")).get("error", {}).get("errors", [])
        return any(str(e.get("reason", "")).lower() in _RATE_LIMIT_REASONS for e in errors)
    except (ValueError, AttributeError):
        return "ratelimitexceeded" in str(exc).lower()


def _execute(request, *, attempts: int = GMAIL_RETRY_ATTEMPTS, base_delay: float = GMAIL_RETRY_BASE_DELAY_SEC):
    """request.execute() з експоненційною паузою (1, 2, 4, 8 с) на ліміти
    квоти; після останньої спроби помилка піднімається як є."""
    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as exc:
            if not _is_retryable(exc) or attempt == attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning("Gmail API: ліміт квоти (спроба %d/%d), пауза %.0f с", attempt + 1, attempts, delay)
            time.sleep(delay)


def _build_query(company_names: list[str]) -> str:
    keyword_clause = " OR ".join(f'"{kw}"' for kw in GMAIL_KEYWORDS)
    company_clause = " OR ".join(f'"{c}"' for c in company_names) if company_names else ""
    hr_clause = "from:hr@*"

    clauses = [f"({keyword_clause})", hr_clause]
    if company_clause:
        clauses.append(f"({company_clause})")

    return f"({' OR '.join(clauses)}) newer_than:{GMAIL_SEARCH_WINDOW_DAYS}d"


def get_or_create_label(name: str = GMAIL_LABEL_NAME) -> str:
    service = gmail_service()
    labels = _execute(service.users().labels().list(userId="me")).get("labels", [])
    for label in labels:
        if label["name"] == name:
            return label["id"]
    created = _execute(
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
    )
    return created["id"]


def search_threads(company_names: list[str], label_id_to_exclude: str) -> list[dict]:
    """Повертає список метаданих тредів, які ще НЕ мають лейблу
    label_id_to_exclude і відповідають трьом умовам з config.GMAIL_KEYWORDS
    / hr@ / company_names.
    """
    service = gmail_service()
    query = _build_query(company_names) + f" -label:{_label_name_safe(label_id_to_exclude)}"
    threads: list[dict] = []
    page_token: str | None = None
    while True:
        resp = _execute(
            service.users().threads().list(userId="me", q=query, pageToken=page_token, maxResults=50)
        )
        threads.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return threads


def _label_name_safe(label_id_or_name: str) -> str:
    # Gmail query syntax -label:<name> хоче назву, не id; для простоти
    # приймаємо тут назву (див. виклик у pipeline/step2_mail.py).
    return label_id_or_name


def get_thread(thread_id: str, *, fmt: str = "full") -> dict:
    """fmt="full" за замовчуванням: Крок 2 класифікує ТІЛО листа, тож
    format="metadata" (лише заголовки, значно дешевше за квотою) тут не
    підходить. Для місць, де тіло не потрібне, передавай fmt="metadata"."""
    service = gmail_service()
    return _execute(service.users().threads().get(userId="me", id=thread_id, format=fmt))


def extract_plain_text(message: dict) -> str:
    """Витягує читабельний текст з Gmail message payload (рекурсивно по
    multipart), пропускаючи HTML-частину коли є text/plain альтернатива.
    """
    payload = message.get("payload", {})

    def walk(part: dict) -> str | None:
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime_type == "text/plain" and data:
            return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
        for sub in part.get("parts", []) or []:
            result = walk(sub)
            if result:
                return result
        if mime_type == "text/html" and data:
            return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
        return None

    return walk(payload) or message.get("snippet", "")


def apply_label(message_id: str, label_id: str) -> None:
    service = gmail_service()
    _execute(
        service.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": [label_id]})
    )


def header_value(message: dict, name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""

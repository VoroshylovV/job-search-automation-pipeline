"""Gmail API: пошук листів за трьома незалежними умовами (ключові слова /
hr@-домени / компанії зі списку), керування лейблом, читання тіла листа."""
from __future__ import annotations

import base64
import logging

from config import GMAIL_KEYWORDS, GMAIL_LABEL_NAME, GMAIL_SEARCH_WINDOW_DAYS
from google_services.auth import gmail_service

logger = logging.getLogger(__name__)


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
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"] == name:
            return label["id"]
    created = (
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
        .execute()
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
        resp = (
            service.users()
            .threads()
            .list(userId="me", q=query, pageToken=page_token, maxResults=50)
            .execute()
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


def get_thread(thread_id: str) -> dict:
    service = gmail_service()
    return service.users().threads().get(userId="me", id=thread_id, format="full").execute()


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
    service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id]}
    ).execute()


def header_value(message: dict, name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""

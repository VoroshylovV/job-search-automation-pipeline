"""Google Docs API — реальне редагування вмісту "на місці".

Це саме та частина, заради якої весь пайплайн переписувався з MCP-чату на
Python: у чат-версії не було операції "онови вміст наявного файлу", і
доводилось emulювати "дописування" через trash+recreate (новий file_id
щодня). Тут — один batchUpdate виклик, той самий file_id, повна історія
версій Google Docs зберігається.
"""
from __future__ import annotations

from google_services.auth import docs_service
from google_services.drive import guard_not_forbidden


def read_full_text(document_id: str) -> str:
    service = docs_service()
    doc = service.documents().get(documentId=document_id).execute()
    text_parts: list[str] = []
    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if text_run:
                text_parts.append(text_run.get("content", ""))
    return "".join(text_parts)


def _end_index(service, document_id: str) -> int:
    doc = service.documents().get(documentId=document_id).execute()
    return doc["body"]["content"][-1]["endIndex"]


def replace_full_text(document_id: str, new_text: str) -> None:
    """Повністю замінює вміст документа. Це НАСТОЯЩЕ редагування на місці —
    file_id, права доступу й посилання лишаються тими самими.

    Аналог того, що в MCP-версії промпту довелось описувати як
    "прочитати -> сформувати повний новий вміст -> trash старий файл ->
    створити новий" — тут це один API-виклик без жодних побічних ефектів.
    """
    guard_not_forbidden(document_id)
    service = docs_service()
    end_index = _end_index(service, document_id)

    requests_batch = []
    # Google Docs: не можна видалити ВЕСЬ вміст (мінімум лишається порожній
    # параграф), тому видаляємо [1, end_index - 1), якщо є що видаляти.
    # Порожній документ має end_index == 2 (лише фінальний "\n") — діапазон
    # [1, 1) порожній, і Docs API відхиляє його з HTTP 400 ("The range should
    # not be empty"), тому поріг саме > 2, а не > 1.
    if end_index > 2:
        requests_batch.append(
            {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}}
        )
    if new_text:
        requests_batch.append(
            {"insertText": {"location": {"index": 1}, "text": new_text}}
        )

    if requests_batch:
        service.documents().batchUpdate(
            documentId=document_id, body={"requests": requests_batch}
        ).execute()


def append_text(document_id: str, text_to_append: str) -> None:
    """Справжнє дописування в кінець документа (реальний append, не rebuild)."""
    guard_not_forbidden(document_id)
    service = docs_service()
    end_index = _end_index(service, document_id)
    # insertText з index = end_index - 1 (перед фінальним символом кінця
    # секції) — стандартний трюк Docs API для "додати в кінець".
    insert_index = max(1, end_index - 1)
    service.documents().batchUpdate(
        documentId=document_id,
        body={
            "requests": [
                {"insertText": {"location": {"index": insert_index}, "text": text_to_append}}
            ]
        },
    ).execute()

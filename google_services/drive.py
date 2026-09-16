"""Google Drive API: пошук файлів у папці з резюме + гігієна перед записом.

На відміну від MCP-версії пайплайну (чат-based scheduled task), тут можна
робити НАСТУПНІ речі, яких раніше бракувало:
  - шукати файл за (title, parent) через googleapiclient напряму, без обхідних
    трюків;
  - але головне — Docs/Sheets API (docs.py, sheets.py) дозволяють РЕАЛЬНЕ
    редагування "на місці" (append/batchUpdate), а не
    trash+recreate-workaround, який був у MCP-версії.
"""
from __future__ import annotations

import logging

from config import FORBIDDEN_FILE_IDS, RESUME_FOLDER_ID
from google_services.auth import drive_service

logger = logging.getLogger(__name__)


class ForbiddenFileError(Exception):
    """Спроба записати у файл зі списку FORBIDDEN_FILE_IDS (config.py)."""


def guard_not_forbidden(file_id: str) -> None:
    if file_id in FORBIDDEN_FILE_IDS:
        raise ForbiddenFileError(
            f"Відмовляюсь писати у файл {file_id} — {FORBIDDEN_FILE_IDS[file_id]}. "
            "Це навмисне обмеження (config.FORBIDDEN_FILE_IDS), не баг."
        )


def list_folder_files(folder_id: str = RESUME_FOLDER_ID) -> list[dict]:
    """Повертає [{id, name, mimeType}, ...] для всіх файлів у папці
    (без підпапок), включно зі службовими файлами пайплайну — фільтрація
    "це не резюме, а службовий файл" — відповідальність викликаючого коду
    (pipeline/step2_mail.py), а не цієї low-level функції.
    """
    service = drive_service()
    files: list[dict] = []
    page_token: str | None = None
    query = f"'{folder_id}' in parents and trashed = false"
    while True:
        resp = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
                pageSize=100,
            )
            .execute()
        )
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def find_file_by_title(title: str, folder_id: str = RESUME_FOLDER_ID) -> dict | None:
    service = drive_service()
    safe_title = title.replace("'", "\\'")
    query = (
        f"'{folder_id}' in parents and trashed = false "
        f"and name = '{safe_title}'"
    )
    resp = service.files().list(q=query, fields="files(id, name, mimeType)").execute()
    files = resp.get("files", [])
    return files[0] if files else None


def create_google_doc(title: str, folder_id: str = RESUME_FOLDER_ID) -> str:
    """Створює порожній Google Doc, повертає file_id."""
    service = drive_service()
    file_metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [folder_id],
    }
    created = service.files().create(body=file_metadata, fields="id").execute()
    return created["id"]


def create_spreadsheet(title: str, folder_id: str = RESUME_FOLDER_ID) -> str:
    """Створює порожню Google Sheet, повертає file_id."""
    service = drive_service()
    file_metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [folder_id],
    }
    created = service.files().create(body=file_metadata, fields="id").execute()
    return created["id"]

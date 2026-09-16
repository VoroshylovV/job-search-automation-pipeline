"""Google Sheets API — реальний append_row, без CSV-екранування і без
перестворення файлу. Це друга частина обіцяної переваги Python-версії
над MCP-чатом: там доводилось вручну екранувати коми/лапки в CSV перед
перезбіркою файлу — тут кожне значення йде в комірку як є, Sheets API
сам відповідає за коректне збереження будь-якого тексту.
"""
from __future__ import annotations

from config import METRICS_SHEET_TITLE
from google_services.auth import sheets_service
from google_services.drive import guard_not_forbidden
from models import METRICS_HEADER

DEFAULT_RANGE = "A1"


def ensure_header(spreadsheet_id: str, header: list[str] = METRICS_HEADER) -> None:
    guard_not_forbidden(spreadsheet_id)
    service = sheets_service()
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range="A1:O1")
        .execute()
    )
    existing = result.get("values", [])
    if existing and existing[0] == header:
        return
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="A1",
        valueInputOption="RAW",
        body={"values": [header]},
    ).execute()


def append_row(spreadsheet_id: str, row: list) -> None:
    guard_not_forbidden(spreadsheet_id)
    service = sheets_service()
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=DEFAULT_RANGE,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()

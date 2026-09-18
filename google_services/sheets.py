"""Google Sheets API — реальний append_row / values.get, без CSV-екранування
і без перестворення файлу. Це друга частина обіцяної переваги Python-версії
над MCP-чатом: там доводилось вручну екранувати коми/лапки в CSV перед
перезбіркою файлу — тут кожне значення йде в комірку як є, Sheets API сам
відповідає за коректне збереження будь-якого тексту.
"""
from __future__ import annotations

import string

from config import METRICS_SHEET_TITLE
from google_services.auth import sheets_service
from google_services.drive import guard_not_forbidden
from models import METRICS_HEADER

DEFAULT_RANGE = "A1"


def _col_letter(n: int) -> str:
    """1-indexed кількість колонок -> буква останньої колонки (A, B, ..., Z, AA, ...)."""
    letters = string.ascii_uppercase
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = letters[rem] + result
    return result


def ensure_header(spreadsheet_id: str, header: list[str] = METRICS_HEADER) -> None:
    guard_not_forbidden(spreadsheet_id)
    service = sheets_service()
    last_col = _col_letter(len(header))
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"A1:{last_col}1")
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


def read_values(spreadsheet_id: str, range_: str = "A1:ZZ") -> list[list[str]]:
    """Читання БЕЗ guard_not_forbidden — читати "Мої відгуки на вакансію.xlsx"
    заборони немає, заборонено лише писати в неї (drive.guard_not_forbidden
    застосовується явно в ensure_header/append_row, тут навмисно ні)."""
    service = sheets_service()
    result = (
        service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_).execute()
    )
    return result.get("values", [])


def read_column_by_header(spreadsheet_id: str, column_header: str) -> list[str]:
    """Знаходить колонку за назвою в рядку заголовків (рядок 1) і повертає
    всі непорожні значення під нею. Використовується для Рівня 2 дедуп-страховки
    (звірка по колонці "Посилання на вакансію" таблиці відгуків) — читання
    без прив'язки до фіксованого номера колонки, бо Володимир іноді
    редагує/перевпорядковує цю таблицю вручну."""
    rows = read_values(spreadsheet_id)
    if not rows:
        return []
    header = rows[0]
    try:
        idx = header.index(column_header)
    except ValueError:
        return []
    values: list[str] = []
    for row in rows[1:]:
        if idx < len(row) and row[idx].strip():
            values.append(row[idx].strip())
    return values


def read_last_data_rows(spreadsheet_id: str, n: int) -> list[list[str]]:
    """Останні до n рядків ДАНИХ (без рядка заголовків), у хронологічному
    порядку (найстаріший з вибраних -> найновіший). Використовується Кроком 4
    (4.3) для евристики по історії "Результат щоденної перевірки"."""
    rows = read_values(spreadsheet_id)
    if len(rows) <= 1:
        return []
    return rows[1:][-n:]

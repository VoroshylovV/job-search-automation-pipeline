"""Діагностичний лог причин відсіву вакансій Кроку 1 (додано 27.09.2026).

Мета — порівняти онлайн- (промт) і десктоп- (цей репозиторій) версії за
URL: хто що відсіяв і на якому критерії. Кожен запуск створює окремий Google
Sheet у підпапці REJECT_LOG_FOLDER_ID з назвою
"Лог відсіву YYYY-MM-DD (тест гітхаб)" і пише всі рядки одним викликом.
Формат колонок (REJECT_LOG_HEADER) спільний з онлайн-версією.
Збій запису не валить запуск — main.py лише додає примітку у звіт.
"""
from __future__ import annotations

from datetime import date

from config import REJECT_LOG_FOLDER_ID, REJECT_LOG_SYSTEM_LABEL
from google_services import drive
from google_services.auth import sheets_service
from models import REJECT_LOG_HEADER


def build_rows(decisions: list[dict], utc_today: date) -> list[list[str]]:
    return [
        [
            utc_today.isoformat(), REJECT_LOG_SYSTEM_LABEL, d["source"], d["url"], d["label"],
            d["result"], d["code"], d["detail"], d["posted_date"], d["date_source"],
        ]
        for d in decisions
    ]


def write_reject_log(decisions: list[dict], utc_today: date) -> str:
    """Створює файл логу і записує заголовок + рядки. Повертає file_id."""
    title = f"Лог відсіву {utc_today.isoformat()} ({REJECT_LOG_SYSTEM_LABEL})"
    file_id = drive.create_spreadsheet(title, REJECT_LOG_FOLDER_ID)
    values = [REJECT_LOG_HEADER] + build_rows(decisions, utc_today)
    sheets_service().spreadsheets().values().update(
        spreadsheetId=file_id, range="A1", valueInputOption="RAW", body={"values": values},
    ).execute()
    return file_id

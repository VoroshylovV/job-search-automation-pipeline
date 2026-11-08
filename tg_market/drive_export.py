"""Вивантаження даних збору Telegram у Google Drive (Google Sheets).

    python -m tg_market.drive_export              # знімок за поточний місяць
    python -m tg_market.drive_export --dry-run    # лише показати, що буде вивантажено

Куди: файл «TG-ринок: дані збору YYYY-MM» у папці Drive, ID якої задано змінною
TG_MARKET_DRIVE_FOLDER_ID у .env (папку створи сам; без змінної модуль нічого не робить).
Що: аркуші posts, vacancies (tg_market/data/) та всі out/*.csv (analyze, compare, monthly).
Повторний запуск у тому самому місяці перезаписує вміст аркушів того ж файлу.
Тексти постів і контакти не вивантажуються — їх немає і в самих CSV.
Після запису кожний аркуш читається назад, кількість рядків звіряється.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

from tg_market.analyze import DATA, OUT

FOLDER_ENV = "TG_MARKET_DRIVE_FOLDER_ID"
RAW_FILES = ("posts.csv", "vacancies.csv")
CHUNK_ROWS = 2000
SHEET_NAME_MAX = 100
_NUM = re.compile(r"^[+-]?\d+([.,]\d+)?$")


def safe_cell(value) -> str:
    """Захист від формул: значення на «=», «@» або «+/-» не-числа Sheets сприйняв би як формулу."""
    s = "" if value is None else str(value)
    if s[:1] in ("=", "@") or (s[:1] in ("+", "-") and not _NUM.match(s)):
        return "'" + s
    return s


def snapshot_title(day: date) -> str:
    return f"TG-ринок: дані збору {day:%Y-%m}"


def collect_tables(data: Path | None = None, out: Path | None = None) -> list[tuple[str, list[list[str]]]]:
    data, out = data or DATA, out or OUT
    paths = [data / name for name in RAW_FILES] + sorted(out.glob("*.csv"))
    tables: list[tuple[str, list[list[str]]]] = []
    for path in paths:
        if not path.exists():
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = [[safe_cell(c) for c in row] for row in csv.reader(f)]
        if rows:
            tables.append((path.stem[:SHEET_NAME_MAX], rows))
    return tables


def _quote(sheet: str) -> str:
    return "'" + sheet.replace("'", "''") + "'"


def upload(tables: list[tuple[str, list[list[str]]]], folder_id: str, title: str) -> tuple[str, str]:
    """Створює або перезаписує файл `title` у папці. Повертає (file_id, url). Піднімає виняток при збої."""
    from google_services import drive
    from google_services.auth import sheets_service

    svc = sheets_service().spreadsheets()
    existing = drive.find_file_by_title(title, folder_id)
    file_id = existing["id"] if existing else drive.create_spreadsheet(title, folder_id)
    drive.guard_not_forbidden(file_id)

    meta = svc.get(spreadsheetId=file_id, fields="sheets.properties(sheetId,title)").execute()
    have = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    names = [n for n, _ in tables]
    requests: list[dict] = []

    def grid(rows: list[list[str]]) -> dict:
        return {"rowCount": max(len(rows) + 20, 100), "columnCount": max(max(len(r) for r in rows), 10), "frozenRowCount": 1}

    todo = dict(tables)
    if not existing and have:  # новий файл: перший порожній аркуш перейменовуємо, щоб не лишався «Sheet1»
        first_title, first_id = next(iter(have.items()))
        if names and names[0] not in have:
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": first_id, "title": names[0], "gridProperties": grid(todo[names[0]])},
                "fields": "title,gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}})
            have = {names[0]: first_id}
    for name in names:
        if name in have:
            if not any(r.get("updateSheetProperties", {}).get("properties", {}).get("sheetId") == have[name] for r in requests):
                requests.append({"updateSheetProperties": {
                    "properties": {"sheetId": have[name], "gridProperties": grid(todo[name])},
                    "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount"}})
        else:
            requests.append({"addSheet": {"properties": {"title": name, "gridProperties": grid(todo[name])}}})
    if requests:
        svc.batchUpdate(spreadsheetId=file_id, body={"requests": requests}).execute()

    svc.values().batchClear(spreadsheetId=file_id, body={"ranges": [_quote(n) for n in names]}).execute()
    for name, rows in tables:
        for start in range(0, len(rows), CHUNK_ROWS):
            svc.values().update(
                spreadsheetId=file_id, range=f"{_quote(name)}!A{start + 1}", valueInputOption="USER_ENTERED",
                body={"values": rows[start:start + CHUNK_ROWS]},
            ).execute()
    for name, rows in tables:  # перевірка: читаємо назад колонку A
        got = svc.values().get(spreadsheetId=file_id, range=f"{_quote(name)}!A:A").execute().get("values", [])
        if len(got) != len(rows):
            raise RuntimeError(f"аркуш {name}: у Drive {len(got)} рядків, локально {len(rows)}")
    return file_id, f"https://docs.google.com/spreadsheets/d/{file_id}/edit"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="лише показати аркуші й кількість рядків")
    args = ap.parse_args(argv)

    tables = collect_tables()
    if not tables:
        print("Немає що вивантажувати: tg_market/data/*.csv порожні або відсутні.")
        return 1
    title = snapshot_title(date.today())
    for name, rows in tables:
        print(f"  {name}: {len(rows) - 1} рядків")
    if args.dry_run:
        print(f"[dry-run] файл «{title}» не створено")
        return 0
    try:
        import config  # noqa: F401  — підтягує .env
    except Exception:  # noqa: BLE001
        pass
    folder_id = os.environ.get(FOLDER_ENV, "").strip()
    if not folder_id:
        print(f"Не задано {FOLDER_ENV} у .env. Створи папку в Google Drive, скопіюй її ID з адреси "
              f"(…/folders/<ID>) і додай рядок {FOLDER_ENV}=<ID> у .env.")
        return 2
    file_id, url = upload(tables, folder_id, title)
    print(f"Готово: «{title}» → {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

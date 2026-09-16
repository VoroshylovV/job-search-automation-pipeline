"""OAuth-автентифікація Google (Drive, Docs, Sheets, Gmail — один спільний
токен, бо всі scopes запитуються разом, див. config.GOOGLE_SCOPES).

Перший запуск: відкриє браузер для логіну Google і збереже token.json.
Наступні запуски (у т.ч. на сервері/в cron) переви користовуються збереженим
токеном і мовчки його оновлюють (refresh_token), поки Google його не відкличе.
"""
from __future__ import annotations

import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from config import GOOGLE_CREDENTIALS_PATH, GOOGLE_SCOPES, GOOGLE_TOKEN_PATH

logger = logging.getLogger(__name__)

_creds_cache: Credentials | None = None


def get_credentials() -> Credentials:
    global _creds_cache
    if _creds_cache and _creds_cache.valid:
        return _creds_cache

    creds: Credentials | None = None
    if os.path.exists(GOOGLE_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, GOOGLE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"Не знайдено {GOOGLE_CREDENTIALS_PATH}. Завантаж OAuth "
                    "client secret з Google Cloud Console (Credentials -> "
                    "Create OAuth client ID -> Desktop app) і поклади сюди. "
                    "Детальніше — README.md, розділ 'Налаштування Google API'."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_PATH, GOOGLE_SCOPES
            )
            # На headless-сервері (cron) це впаде — токен треба один раз
            # згенерувати локально (де є браузер) і скопіювати token.json
            # на сервер. Див. README.
            creds = flow.run_local_server(port=0)

        os.makedirs(os.path.dirname(GOOGLE_TOKEN_PATH) or ".", exist_ok=True)
        with open(GOOGLE_TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    _creds_cache = creds
    return creds


def build_service(name: str, version: str) -> Resource:
    return build(name, version, credentials=get_credentials(), cache_discovery=False)


def drive_service() -> Resource:
    return build_service("drive", "v3")


def docs_service() -> Resource:
    return build_service("docs", "v1")


def sheets_service() -> Resource:
    return build_service("sheets", "v4")


def gmail_service() -> Resource:
    return build_service("gmail", "v1")

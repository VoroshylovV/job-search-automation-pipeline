"""
Централізована конфігурація Job Search Automation Pipeline.

Усі "бізнес-правила", які раніше жили тільки в тексті промпту scheduled-задачі,
тепер живуть тут як звичайні Python-константи — редагувати критерії means
редагувати цей файл, а не переписувати промпт вручну.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # читає .env у робочій директорії, якщо він є

# --------------------------------------------------------------------------
# Секрети / оточення
# --------------------------------------------------------------------------
# Anthropic API ключ читається зі змінної середовища ANTHROPIC_API_KEY
# (див. .env.example). Google credentials — з credentials/credentials.json
# (OAuth client secret, завантажений з Google Cloud Console).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

GOOGLE_CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_CREDENTIALS_PATH", "credentials/credentials.json"
)
GOOGLE_TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "credentials/token.json")

# Scopes, потрібні пайплайну. drive.file обмежує доступ лише файлами,
# створеними/відкритими цим додатком — але оскільки нам треба читати
# наявну папку з резюме, потрібен повний drive.readonly + drive для
# читання/запису службових файлів.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
]

# --------------------------------------------------------------------------
# Google Drive / Docs / Sheets ідентифікатори
# --------------------------------------------------------------------------
RESUME_FOLDER_ID = "1R_IeQaYOxf8EX6J1CUZpUjcvZaFMJ-7n"  # "Резюме для працевлаштування..."

DEDUP_LOG_TITLE = "Лог показаних вакансій (автопошук)"
METRICS_SHEET_TITLE = "Метрики автопошуку вакансій"

# Файл, який пайплайн НІКОЛИ не повинен чіпати — особистий ручний трекер
# співбесід Володимира. Тримаємо тут явно, щоб будь-який код, що працює
# з Drive-файлами, міг звірятися з цим списком-забороною.
FORBIDDEN_FILE_IDS = {
    "1wNsGBUKFvvliEquRufynZrCuxWbEEx8l": "Мої відгуки на вакансію.xlsx (ручний трекер співбесід)",
}

GMAIL_LABEL_NAME = "Знайдено-Пошук-Вакансій"

DEDUP_LOG_MAX_AGE_DAYS = 7  # довше не потрібно: вікно пошуку — 2 дні

# --------------------------------------------------------------------------
# Профіль кандидата
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CandidateProfile:
    name: str = "Володимир Ворошилов"
    location: str = "Zawadzkie, Польща"
    target_roles: tuple[str, ...] = ("Junior Data Analyst", "Junior Product Analyst")
    background: str = (
        "8+ років бекграунду в telecom/ops до переходу в аналітику даних, "
        "пройшов курс GoIT Data Analyst (сертифікат «Пройдено»)"
    )
    real_skills: tuple[str, ...] = (
        "SQL", "Python/Pandas", "Tableau", "EDA", "A/B тестування", "Excel",
    )
    # Навички, які кандидат НЕ має на практиці — не можна зараховувати їх
    # у Match-рівень і не можна приписувати кандидату.
    non_skills: tuple[str, ...] = ("Power BI", "Looker Studio", "R")


CANDIDATE = CandidateProfile()

# --------------------------------------------------------------------------
# Критерії фільтрації вакансій (Крок 1)
# --------------------------------------------------------------------------
MAX_EXPERIENCE_YEARS = 1.5
MIN_SALARY_UAH = 40_000
MAX_ENGLISH_LEVEL = "B2"  # виключає C1+
DATE_WINDOW_DAYS = 2  # "сьогодні або вчора"
UNKNOWN_DATE_FALLBACK_LIMIT = 50  # скільки вакансій перевіряти, якщо дата невідома

SOURCES = ("djinni.co", "jobs.dou.ua", "robota.ua", "work.ua", "happymonday.ua")

# --------------------------------------------------------------------------
# Крок 2 — пошта
# --------------------------------------------------------------------------
GMAIL_SEARCH_WINDOW_DAYS = 30
GMAIL_KEYWORDS = (
    "вакансія", "співбесіда", "candidate", "interview", "resume", "application",
)
# Токени, які потрібно виключити з назви файлу при витягуванні назви компанії
FILENAME_NOISE_TOKENS = (
    "cv", "resume", "cover", "cover_letter", "cover letter", "voroshylov",
    "ворошилов", "data", "analyst", "business", "marketing", "junior",
    "middle", "senior", "product",
)

EMAIL_STATUS_OPTIONS = (
    "відгук на розгляді",
    "запрошення на співбесіду",
    "відмова",
    "headhunting-пропозиція (кандидат не подавався)",
    "очікування без явної відповіді",
)

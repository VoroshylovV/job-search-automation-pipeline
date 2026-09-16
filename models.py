"""Типізовані структури даних, спільні для всього пайплайну."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawJobPosting:
    """Сира вакансія, як її зібрав скрапер — ДО будь-якої семантичної оцінки.

    Claude (не скрапер) вирішує, чи відповідає вона критеріям — скрапер лише
    збирає кандидатів за поверхневими ключовими словами (щоб не тягнути
    весь сайт).
    """

    source: str  # "djinni.co" | "jobs.dou.ua" | "robota.ua" | "work.ua" | "happymonday.ua"
    title: str
    company: str
    url: Optional[str]
    posted_raw: str  # текст як є на сайті: "3d ago", "15 вересня", "" тощо
    salary_raw: str
    description_snippet: str
    location_raw: str = ""


@dataclass
class ScoredVacancy:
    """Вакансія після оцінки Claude — те, що йде у фінальний список Кроку 1."""

    title: str
    company: str
    source: str
    url: Optional[str]
    match_level: str  # "High" | "Medium" | "Low"
    match_reasoning: str
    veteran_bonus: bool
    low_match_location: bool
    low_match_location_reason: str
    posted_date: Optional[str]  # ISO-дата, або None якщо невизначена
    date_undetermined: bool

    def dedup_key(self) -> str:
        if self.url:
            return self.url
        return f"{self.title.strip().lower()}|{self.company.strip().lower()}"


@dataclass
class EmailFinding:
    thread_id: str
    message_id: str
    sender: str
    subject: str
    date: str
    summary: str
    status: Optional[str] = None  # одне з config.EMAIL_STATUS_OPTIONS, або None
    from_hr_domain: bool = False
    from_known_company: bool = False


@dataclass
class SourceStatus:
    source: str
    status: str = "OK"  # "OK" | "недоступне" | "дата невизначена"
    note: str = ""


@dataclass
class RunMetrics:
    run_date: str
    total_found_before_filters: int = 0
    shown_after_filters: int = 0
    low_match_location_count: int = 0
    veteran_bonus_count: int = 0
    undetermined_date_count: int = 0
    total_emails_found: int = 0
    hr_domain_emails: int = 0
    known_company_emails: int = 0
    source_statuses: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def as_row(self) -> list:
        s = self.source_statuses
        return [
            self.run_date,
            self.total_found_before_filters,
            self.shown_after_filters,
            self.low_match_location_count,
            self.veteran_bonus_count,
            self.undetermined_date_count,
            self.total_emails_found,
            self.hr_domain_emails,
            self.known_company_emails,
            s.get("djinni.co", "OK"),
            s.get("jobs.dou.ua", "OK"),
            s.get("robota.ua", "OK"),
            s.get("work.ua", "OK"),
            s.get("happymonday.ua", "OK"),
            self.notes or "без зауважень",
        ]


METRICS_HEADER = [
    "Дата запуску",
    "Загальна кількість вакансій, знайдених на всіх джерелах до фільтрації",
    "Показані вакансії (пройшли всі фільтри)",
    "Low match через локацію (кількість)",
    "Ветеранські бонуси (кількість)",
    "Невизначена дата публікації (кількість)",
    "Нові листи від роботодавців (Крок 2)",
    "Листи від hr@ доменів (headhunting)",
    "Листи від компаній зі списку Drive-папки",
    "Статус djinni.co",
    "Статус jobs.dou.ua",
    "Статус robota.ua",
    "Статус work.ua",
    "Статус happymonday.ua",
    "Примітки",
]

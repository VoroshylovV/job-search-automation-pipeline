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


# --------------------------------------------------------------------------
# Крок 1.2 — фріланс-проєкти (Freelancehunt + Telegram-канал)
# --------------------------------------------------------------------------
@dataclass
class RawFreelanceProject:
    """Сирий фріланс-проєкт/пост, ДО оцінки релевантності Claude.

    На відміну від RawJobPosting (5 джерел, спільна структура), тут поля
    трохи асиметричні між джерелами: bids_count і budget_raw мають сенс
    лише для Freelancehunt (2 категорії), для Telegram-каналу вони завжди
    порожні/None — див. коментар у config.py про обмеження джерела 3."""

    source: str  # "freelancehunt.com" | "telegram"
    title: str
    url: str
    category: str  # назва категорії Freelancehunt, або "Telegram" для джерела 3
    posted_raw: str
    bids_count: Optional[int] = None  # лише Freelancehunt
    budget_raw: str = ""  # лише Freelancehunt ("не вказано" теж сюди)
    description_snippet: str = ""


@dataclass
class ScoredFreelanceProject:
    """Проєкт після оцінки Claude. На відміну від ScoredVacancy тут НЕМАЄ
    градації Match-рівня (High/Medium/Low) — лише бінарна релевантність
    (так/ні), як і в текстовій версії промпту: "без градації рівнів на
    кшталт Match-рівня вакансій — тут просто так/ні, однаковий принцип
    для всіх трьох джерел"."""

    title: str
    source: str
    category: str
    url: str
    why_relevant: str  # 1 речення: який саме дотик до аналізу даних/SQL/BI
    posted_date: Optional[str]
    bids_count: Optional[int] = None
    competition_level: Optional[str] = None  # "низька"|"середня"|"висока", лише Freelancehunt
    budget_raw: str = ""

    def dedup_key(self) -> str:
        return self.url


@dataclass
class FreelanceRunMetrics:
    """Одна колонка на кожне з трьох джерел ("дні поспіль з 0") — щоб
    сигнал "джерело малоактивне 7+ днів поспіль" (config.py) можна було
    рахувати з історії файлу, так само як евристика Кроку 4 для вакансій."""

    run_date: str
    shown_bi: int = 0
    shown_sql: int = 0
    shown_total: int = 0
    rejected_irrelevant: int = 0
    status_bi: str = "OK"
    status_sql: str = "OK"
    streak_zero_bi: int = 0
    streak_zero_sql: int = 0
    notes: str = ""
    shown_telegram: int = 0
    status_telegram: str = "OK"
    streak_zero_telegram: int = 0

    def as_row(self) -> list:
        return [
            self.run_date,
            self.shown_bi,
            self.shown_sql,
            self.shown_total,
            self.rejected_irrelevant,
            self.status_bi,
            self.status_sql,
            self.streak_zero_bi,
            self.streak_zero_sql,
            self.notes,
            self.shown_telegram,
            self.status_telegram,
            self.streak_zero_telegram,
        ]


FREELANCE_METRICS_HEADER = [
    "Дата запуску",
    "Показано (BI и аналитика данных)",
    "Показано (Базы данных и SQL)",
    "Показано разом",
    "Відхилено як нерелевантні",
    "Статус BI и аналитика данных",
    "Статус Базы данных и SQL",
    "Дні поспіль з 0 (BI и аналитика данных)",
    "Дні поспіль з 0 (Базы данных и SQL)",
    "Примітки",
    "Показано (Telegram-канал)",
    "Статус Telegram-каналу",
    "Дні поспіль з 0 (Telegram-канал)",
]


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


@dataclass
class SelfCheckResult:
    """Крок 4 — самоперевірка запуску. Лише фіксує вже відомі факти цього
    запуску (+ одне читання власної історії для евристики 4.3) — нічого не
    перераховує заново на джерелах і не змінює файли Кроків 1-3."""

    timestamp_utc: str  # "YYYY-MM-DD HH:MM UTC", той самий момент і для 4.4, і для 4.5
    step1_status: str  # "OK" | "ЧАСТКОВО" | "НЕ ВИКОНАНО"
    step1_note: str
    step2_status: str
    step2_note: str
    step3_metrics_status: str
    step3_metrics_note: str
    step3_tracker_status: str
    step3_tracker_note: str
    sources_ok_count: int  # X/5
    collection_method: str  # "усі_сторінки" | "перша_сторінка" | "оцінка" | "не_встановлено"
    conversion_pct: str  # "NN" або "н/д" (якщо знайдено=0)
    trend_comparable: bool
    trend_comparable_reason: str
    # "так"/"ні"/"н/д" — чи цей запуск при повному 5/5 покритті показав лише
    # Low-match вакансії (або нуль). Зберігається в окремій колонці саме
    # для того, щоб 4.3-евристику (3+ поспіль) можна було рахувати з
    # ІСТОРІЇ файлу, а не лише для поточного запуску — колонок метрики
    # (Крок 3) для цього не досить, там немає розподілу по Match-рівнях.
    only_low_or_zero_at_full_coverage: str
    low_match_streak_signal: bool
    notes: str = ""
    # Крок 1.2 (фріланс) — статус + примітка, окремою колонкою в кінці
    # таблиці (як і в текстовій версії промпту: нова колонка додається в
    # кінець, щоб не зсувати вже наявні історичні рядки).
    step1_2_status: str = "НЕ ВИКОНАНО"
    step1_2_note: str = ""

    def as_row(self) -> list:
        return [
            self.timestamp_utc,
            f"{self.step1_status}" + (f" — {self.step1_note}" if self.step1_note else ""),
            f"{self.step2_status}" + (f" — {self.step2_note}" if self.step2_note else ""),
            f"{self.step3_metrics_status}"
            + (f" — {self.step3_metrics_note}" if self.step3_metrics_note else ""),
            f"{self.step3_tracker_status}"
            + (f" — {self.step3_tracker_note}" if self.step3_tracker_note else ""),
            f"{self.sources_ok_count}/5",
            self.collection_method,
            self.conversion_pct,
            ("так" if self.trend_comparable else "ні")
            + (f" ({self.trend_comparable_reason})" if not self.trend_comparable and self.trend_comparable_reason else ""),
            self.only_low_or_zero_at_full_coverage,
            self.notes or "без зауважень",
            f"{self.step1_2_status}" + (f" — {self.step1_2_note}" if self.step1_2_note else ""),
        ]


SELFCHECK_HEADER = [
    "Перевірка",
    "Крок 1",
    "Крок 2",
    "Крок 3 (метрика)",
    "Крок 3 (таблиця відгуків)",
    "Джерела",
    "Спосіб збору «знайдено»",
    "Конверсія показано/знайдено",
    "Порівнюваність тренду",
    "Лише Low/нуль при 5/5",
    "Примітки",
    "Крок 1.2 (фріланс)",
]

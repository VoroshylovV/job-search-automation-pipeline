"""Скрапери джерел вакансій і фріланс-проєктів. Кожен модуль експортує
функцію scrape() -> Iterator[RawJobPosting | RawFreelanceProject] і кидає
ScraperError (base.py), якщо джерело недоступне — pipeline/step1_vacancies.py
і pipeline/step1_2_freelance.py ловлять це й позначають джерело
"недоступне", не зупиняючи весь запуск."""
from . import djinni, dou, freelancehunt, happymonday, robota, telegram_channel, workua  # noqa: F401

SCRAPER_MODULES = {
    "djinni.co": djinni,
    "jobs.dou.ua": dou,
    "robota.ua": robota,
    "work.ua": workua,
    "happymonday.ua": happymonday,
}

# Крок 1.2 — окремий реєстр, бо freelancehunt.py сам ітерує 2 категорії
# (config.FREELANCEHUNT_CATEGORIES) в одному виклику scrape(), тому тут
# лише 2 записи: одна назва джерела на модуль, не на категорію.
FREELANCE_SCRAPER_MODULES = {
    "freelancehunt.com": freelancehunt,
    "telegram": telegram_channel,
}

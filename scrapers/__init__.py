"""Скрапери джерел вакансій. Кожен модуль експортує функцію scrape() ->
Iterator[RawJobPosting] і кидає ScraperError (base.py), якщо джерело
недоступне — pipeline/step1_vacancies.py ловить це й позначає джерело
"недоступне", не зупиняючи весь запуск."""
from . import djinni, dou, happymonday, robota, workua  # noqa: F401

SCRAPER_MODULES = {
    "djinni.co": djinni,
    "jobs.dou.ua": dou,
    "robota.ua": robota,
    "work.ua": workua,
    "happymonday.ua": happymonday,
}

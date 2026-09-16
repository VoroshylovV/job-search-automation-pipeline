# Job Search Automation Pipeline

Python-версія щоденного пошуку вакансій, яка раніше жила як текстовий промт
scheduled-задачі. Той самий "мозок" (Claude оцінює вакансії й листи), але:

- скрапінг джерел — окремі Python-модулі, а не браузинг через чат;
- Google Docs/Sheets редагуються **на місці** через `documents.batchUpdate` /
  `spreadsheets.values.append` — без trash+recreate;
- лічильники для метрик рахує Python (`len()` над списками), а не LLM "по
  ходу" — структурно неможливо збитись з рахунку на великих обсягах;
- hr@-домен і "компанія зі списку" визначаються регексом у Python — Claude
  відповідає тільки за те, що код визначити не може (семантика).

## Структура

```
config.py                 — усі бізнес-правила (профіль, пороги, назви файлів)
models.py                 — dataclasses: RawJobPosting, ScoredVacancy, EmailFinding, RunMetrics
scrapers/                 — по одному модулю на джерело (djinni, dou, robota, workua, happymonday)
google_services/          — auth.py, drive.py, docs.py, sheets.py, gmail.py
claude_orchestrator/      — prompts.py (тексти), client.py (виклик Anthropic API)
pipeline/                 — step1_vacancies.py, step2_mail.py, step3_metrics.py
main.py                   — точка входу, формує фінальний звіт (report_<дата>.md)
```

## Встановлення

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # і заповни ANTHROPIC_API_KEY
```

## Налаштування Google API

1. Google Cloud Console → створи проєкт (або використай наявний) →
   увімкни APIs: **Google Drive API**, **Google Docs API**, **Google Sheets
   API**, **Gmail API**.
2. APIs & Services → Credentials → Create Credentials → OAuth client ID →
   Application type: **Desktop app**. Завантаж JSON, поклади як
   `credentials/credentials.json`.
3. Перший запуск `python main.py` відкриє браузер для логіну Google —
   увійди тим акаунтом, де лежить папка з резюме. Після логіну збережеться
   `credentials/token.json` (автооновлюється, повторний логін не потрібен,
   поки Google не відкличе доступ).
4. **Для headless-сервера/cron**: крок 3 вимагає браузера, тому token.json
   треба один раз згенерувати на машині з браузером (наприклад, локально)
   і скопіювати `credentials/token.json` на сервер. Далі пайплайн працює
   без інтерактивного логіну.

`credentials/` — обов'язково додай у `.gitignore`, там лежать секрети.

## Запуск

```bash
python main.py
```

Виводить звіт у stdout і зберігає в `reports/report_<YYYY-MM-DD>.md`.

## Планування запуску

Будь-який з варіантів — обери зручний:

- **cron** (Linux/macOS): `0 9 * * * cd /path/to/job_search_pipeline && venv/bin/python main.py >> logs/cron.log 2>&1`
- **systemd timer** — якщо потрібен більш керований запуск із логами.
- **GitHub Actions** (`schedule: cron: '0 9 * * *'`) — зручно, якщо код у
  приватному репозиторії; секрети (ANTHROPIC_API_KEY, вміст token.json) —
  через GitHub Secrets.

## Якщо requests+BeautifulSoup не бачить вакансій (SPA-сайти)

`scrapers/robota.py` уже має запасний варіант через вбудований JSON-стан
сторінки. Якщо і це не спрацює на практиці (сайт повністю рендериться
клієнтським JS без початкового стану в HTML) — заміни `requests.get` у
конкретному скрапері на `playwright` (`pip install playwright && playwright
install chromium`), не чіпаючи решту пайплайна — контракт `scrape() ->
Iterator[RawJobPosting]` лишається той самий.

## Важливі гарантії, збережені з текстової версії промпту

- **НІКОЛИ не пише** у `Мої відгуки на вакансію.xlsx` (id у
  `config.FORBIDDEN_FILE_IDS`) — `google_services/drive.py::guard_not_forbidden`
  кидає виключення при спробі, це не забудеш прибрати випадково.
- Дедублікація вакансій — 7-денне вікно логу, окремий файл "Лог показаних
  вакансій (автопошук)" у тій самій Drive-папці, що й резюме.
- Крок 2 виконується завжди, незалежно від результату Кроку 1 (`main.py`
  ловить помилку Кроку 1 окремо й не блокує Крок 2/3).
- Джерело недоступне → позначається "недоступне" в метриках, решта джерел
  все одно перевіряється.

## Відомі обмеження й що варто перевірити перед першим "бойовим" запуском

1. **CSS-селектори в `scrapers/*.py` — орієнтовні.** Пісочниця розробки не
   мала прямого доступу в інтернет (тільки через окремий інструмент
   рендерингу), тому селектори не звірені з живим HTML. Перед продакшеном
   запусти кожен скрапер окремо (`python -m scrapers.djinni` тощо) і
   перевір, чи знаходяться вакансії.
2. Claude API — платний виклик за кожен запуск (батчі по ~40 вакансій на
   виклик для Кроку 1, один виклик для Кроку 2). Онови `CLAUDE_MODEL` у
   `.env`, якщо потрібна інша модель.
3. Gmail query `_build_query` у `google_services/gmail.py` використовує
   `from:hr@*` — це грубе наближення; звузь за потреби.

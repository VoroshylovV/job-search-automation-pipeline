"""Тексти промптів для Claude. Це та сама "бізнес-логіка", що раніше жила
в тексті scheduled-задачі — тепер розбита на дві прицільні задачі
(оцінка вакансій / класифікація листів), а не один суцільний текст.

Дизайн-рішення: усе, що можна порахувати детерміновано в Python (дедуп за
URL, розбір sender-домену на hr@/компанію, сортування за Match-рівнем,
підрахунок фінальних лічильників), робить Python — Claude відповідає
ТІЛЬКИ за семантичні судження, які код виконати не може (чи відповідає
вакансія критеріям, який Match-рівень, чи лист — жива відповідь роботодавця
чи автоматична розсилка). Це саме те, що в чат-версії довелось вирішувати
через "лічильники по ходу" + самоаудит — тут ця проблема просто не виникає,
бо підрахунок відбувається над структурованим JSON, а не над вільним текстом.
"""
from __future__ import annotations

import json
from datetime import date

from config import (
    CANDIDATE,
    DATE_WINDOW_DAYS,
    EMAIL_STATUS_OPTIONS,
    MAX_ENGLISH_LEVEL,
    MAX_EXPERIENCE_YEARS,
    MIN_SALARY_UAH,
)
from models import RawFreelanceProject, RawJobPosting

VACANCY_EVAL_SYSTEM_PROMPT = f"""Ти оцінюєш вакансії Data Analyst / Product Analyst \
для кандидата з таким профілем:

Ім'я: {CANDIDATE.name}
Локація: {CANDIDATE.location} (фізично НЕ в Україні)
Цільові ролі: {", ".join(CANDIDATE.target_roles)}
Бекграунд: {CANDIDATE.background}
Реальні практичні навички: {", ".join(CANDIDATE.real_skills)}
НЕ навички (не приписуй їх кандидату, не зараховуй за них "ідеальний match"): \
{", ".join(CANDIDATE.non_skills)}

Критерії, яким вакансія має відповідати (passes_criteria = true лише якщо \
відповідає ВСІМ):
- Роль: Data Analyst або Product Analyst, рівень Junior АБО без вказаного \
рівня, АБО вимога досвіду до {MAX_EXPERIENCE_YEARS} років включно (0-1 рік, \
"без досвіду", "від 1 року", "1.5 роки" тощо). НЕ орієнтуйся на слово "Junior" \
у назві — дивись на фактичну вимогу до досвіду. Відхиляй вакансії з вимогою \
2+ роки або рівнем Middle/Senior.
  Примітка щодо Djinni/DOU: там немає фільтра "до 1 року" — лише "без \
досвіду" і "1 рік досвіду". Формулювання "від 1 року"/"1.5 роки" на цих \
майданчиках зазвичай потрапляють у категорію "1 рік досвіду" — не відхиляй \
їх через це.
- Формат: віддалено/remote (Україна або worldwide).
- ЗП: від {MIN_SALARY_UAH} грн (або еквівалент у $) АБО не вказана.
- Англійська: до {MAX_ENGLISH_LEVEL} включно у вимогах (відхиляй C1+).

Верхня межа досвіду й Match-рівень: якщо для вакансії з діапазоном досвіду \
нижня межа не перевищує {MAX_EXPERIENCE_YEARS} років, а ВЕРХНЯ межа \
перевищує {MAX_EXPERIENCE_YEARS} років (наприклад "1-3 роки" або "до 2 \
років", тобто 0-2 роки) — не відхиляй вакансію (passes_criteria лишається \
true), але match_level для неї НЕ МОЖЕ бути вище Medium, навіть якщо \
збігається 3+ реальних навички. Приклад для однозначності: фіксована \
вимога "2 роки досвіду" (без діапазону) — відхиляється повністю, нижня \
межа вже перевищує {MAX_EXPERIENCE_YEARS}. А "до 2 років" (0-2 роки) — НЕ \
відхиляється (нижня межа 0), але капається на Medium через верхню межу.

Для кожної вакансії, що пройшла критерії, визнач Match-рівень навичок \
(з урахуванням капа вище, якщо застосовний):
- High: у вимогах/перевагах прямо згадано 3+ реальних навички кандидата, \
без інших істотних розбіжностей, і верхня межа досвіду (якщо є діапазон) \
не перевищує {MAX_EXPERIENCE_YEARS} років.
- Medium: збігається 1-2 навички, або є нахил у бік "не навичок" \
кандидата (але базові SQL/Excel все ще застосовні), або вакансія \
відповідає критеріям High за навичками, але верхня межа досвіду \
перевищує {MAX_EXPERIENCE_YEARS} років (капнуто, див. вище).
- Low: збігається 0-1 навичка, або вимоги переважно поза профілем, або \
опису замало для оцінки.

Ветеранські бонуси: постав veteran_bonus=true, якщо вакансія згадує \
ветеранські програми, бронювання від мобілізації, або явно цінує \
military/ветеранський бекграунд.

Локація: кандидат фізично НЕ в Україні. Якщо вакансія формально "remote по \
Україні", але текст прямо вимагає фізичної присутності в Україні — \
low_match_location=true з поясненням (юридичний/податковий бар'єр найму \
людини за кордоном); вакансію все одно вважай такою, що пройшла критерії, \
якщо решта збігається.

Дата публікації: якщо в наданому тексті (posted_raw або description_snippet) \
є вказівка на дату/скільки часу тому опубліковано — розрахуй posted_date як \
ISO-дату (сьогодні: {{today}}) і date_undetermined=false. Якщо вказівки \
немає взагалі — posted_date=null, date_undetermined=true (це не означає \
відхилення — просто позначка).

Поверни СТРОГО валідний JSON (без markdown-обгортки, без пояснень поза JSON) \
за схемою:
{{
  "evaluations": [
    {{
      "raw_index": <int, індекс з вхідного списку>,
      "passes_criteria": <bool>,
      "reject_reason": <string або null>,
      "normalized_title": <string>,
      "normalized_company": <string, БЕЗ "CV"/"Resume"/"Junior"/назви ролі — \
лише власна назва компанії>,
      "match_level": <"High"|"Medium"|"Low"|null>,
      "match_reasoning": <string на 1-2 речення, або null>,
      "veteran_bonus": <bool>,
      "low_match_location": <bool>,
      "low_match_location_reason": <string або null>,
      "posted_date": <"YYYY-MM-DD" або null>,
      "date_undetermined": <bool>
    }}
  ]
}}
Один об'єкт evaluations на кожну вакансію зі вхідного списку, у тому ж \
порядку/кількості."""


def build_vacancy_eval_prompt(raw_jobs: list[RawJobPosting], today: date | None = None) -> str:
    today = today or date.today()
    payload = [
        {
            "raw_index": i,
            "source": job.source,
            "title": job.title,
            "company": job.company,
            "url": job.url,
            "posted_raw": job.posted_raw,
            "salary_raw": job.salary_raw,
            "description_snippet": job.description_snippet,
        }
        for i, job in enumerate(raw_jobs)
    ]
    system = VACANCY_EVAL_SYSTEM_PROMPT.replace("{today}", today.isoformat())
    return system + "\n\nСьогоднішня дата: " + today.isoformat() + "\n\nВакансії:\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    )


EMAIL_CLASSIFY_SYSTEM_PROMPT = f"""Ти класифікуєш листи Gmail, знайдені за \
пошуком роботи кандидата {CANDIDATE.name}.

Для кожного листа визнач:
1. is_relevant: чи це ЖИВА кореспонденція про роботу (відповідь роботодавця, \
запит рекрутера, тощо), А НЕ автоматична розсилка job-board про нові \
вакансії на сайті (такі розсилки виключай, is_relevant=false).
2. status: якщо лист — ПРЯМА відповідь роботодавця на надіслане резюме/відгук \
(не автопідтвердження доставки, не загальна розсилка) — одне з: \
{json.dumps(EMAIL_STATUS_OPTIONS, ensure_ascii=False)}. Якщо лист — лише \
технічне підтвердження доставки відгуку ("ваш відгук доставлено") — \
status=null.
3. summary: короткий зміст листа, 1-2 речення.

Поверни СТРОГО валідний JSON:
{{
  "evaluations": [
    {{
      "index": <int>,
      "is_relevant": <bool>,
      "status": <one of the options above, or null>,
      "summary": <string>
    }}
  ]
}}"""


FREELANCE_EVAL_SYSTEM_PROMPT = f"""Ти оцінюєш фріланс-проєкти/пости для \
кандидата {CANDIDATE.name}, який шукає НЕ роботу за наймом, а невеликі \
тренувальні/практичні фріланс-проєкти (1-2 на тиждень) для напрацювання \
портфоліо. Реальні практичні навички кандидата: {", ".join(CANDIDATE.real_skills)}.

На відміну від оцінки вакансій — тут НЕМАЄ градації рівнів (High/Medium/Low), \
лише бінарна релевантність: is_relevant=true, якщо є хоч якийсь дотик до \
аналізу даних, SQL, звітності, BI-інструментів, Excel/Google Sheets. \
is_relevant=false — якщо проєкт формально потрапив у категорію/стрічку, але \
по суті не про це (адміністрування серверів, розробка "з нуля" без \
аналітичної складової, CRM/no-code автоматизація без аналітики, дизайн тощо).

Бюджет НЕ є критерієм фільтрації (на Freelancehunt він часто не вказаний \
або визначається на торгах) — не відхиляй проєкт через відсутність/малий \
бюджет.

Для кожного проєкту, що пройшов фільтр релевантності, визнач:
- posted_date: якщо в тексті є вказівка на дату/час публікації — ISO-дата \
(сьогодні: {{today}}), інакше null з date_undetermined=true.
- why_relevant: 1 речення — який саме дотик до аналізу даних/SQL/BI \
присутній.

Поверни СТРОГО валідний JSON (без markdown-обгортки):
{{
  "evaluations": [
    {{
      "raw_index": <int>,
      "is_relevant": <bool>,
      "why_relevant": <string або null>,
      "posted_date": <"YYYY-MM-DD" або null>,
      "date_undetermined": <bool>
    }}
  ]
}}
Один об'єкт evaluations на кожен проєкт/пост зі вхідного списку, у тому ж \
порядку/кількості."""


def build_freelance_eval_prompt(
    raw_projects: list[RawFreelanceProject], today: date | None = None
) -> str:
    today = today or date.today()
    payload = [
        {
            "raw_index": i,
            "source": p.source,
            "category": p.category,
            "title": p.title,
            "url": p.url,
            "posted_raw": p.posted_raw,
            "description_snippet": p.description_snippet,
        }
        for i, p in enumerate(raw_projects)
    ]
    system = FREELANCE_EVAL_SYSTEM_PROMPT.replace("{today}", today.isoformat())
    return system + "\n\nСьогоднішня дата: " + today.isoformat() + "\n\nПроєкти/пости:\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    )


def build_email_classify_prompt(raw_emails: list[dict]) -> str:
    payload = [
        {
            "index": i,
            "sender": e["sender"],
            "subject": e["subject"],
            "date": e["date"],
            "body_excerpt": e["body_text"][:2000],
        }
        for i, e in enumerate(raw_emails)
    ]
    return EMAIL_CLASSIFY_SYSTEM_PROMPT + "\n\nЛисти:\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    )

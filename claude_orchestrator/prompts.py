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

Для кожної вакансії, що пройшла критерії, визнач три компоненти Match-рівня, \
кожен 0-2 бали (поверни їх у match_scores; підсумковий рівень рахує код, а \
match_level став за тією ж формулою):
- A) Скіли (вимоги/навички): 2 = 3+ реальних навичок кандидата, серед них SQL \
або Python; 1 = 1-2 навички; 0 = жодної або лише з "НЕ навичок" \
(Power BI/Looker Studio/R/GA4).
- B) Обов'язки (що робитимеш): 2 = понад половину ключових обов'язків у \
профілі кандидата; 1 = частково; 0 = переважно поза профілем (розробка/ETL, \
тестування ПЗ, веб-аналітика тегів, продажі, адміністрування).
- C) Очікування роботодавця (рівень/досвід/мова/must-have): 2 = досвід \
<= {MAX_EXPERIENCE_YEARS} року або не вказано, англійська <= {MAX_ENGLISH_LEVEL} або не \
вказана, немає must-have поза профілем; 1 = досвід > {MAX_EXPERIENCE_YEARS} року АБО 1 \
must-have поза профілем; 0 = 2+ must-have поза профілем або вимоги суттєво \
вищі за Junior.
Підсумок (сума A+B+C): High = 5-6 і A>0, B>0; Medium = 3-4; Low = 0-2 АБО \
A=0 АБО B=0. Кап вище (верхня межа досвіду > {MAX_EXPERIENCE_YEARS} року -> \
максимум Medium) лишається чинним незалежно від суми. Якщо повної сторінки \
немає (full_text_available=false) — не вище Medium.

Ветеранські бонуси: постав veteran_bonus=true, якщо вакансія згадує \
ветеранські програми, бронювання від мобілізації, або явно цінує \
military/ветеранський бекграунд.

Військова служба: вакансії у військових частинах/формуваннях (ЗСУ, \
Національна гвардія/НГУ, ДПСУ, інші силові структури) та вакансії, що самі \
по собі передбачають військову службу або військовий контракт — passes_criteria=\
false, reject_code "військова служба", НЕ показуй навіть із low_match_location. \
low_match_location=true — лише для цивільних вакансій із формальним \
обмеженням "тільки для тих, хто фізично перебуває в Україні".

Локація: кандидат фізично НЕ в Україні. Якщо вакансія формально "remote по \
Україні", але текст прямо вимагає фізичної присутності в Україні — \
low_match_location=true з поясненням (юридичний/податковий бар'єр найму \
людини за кордоном); вакансію все одно вважай такою, що пройшла критерії, \
якщо решта збігається.

Дата публікації: якщо в наданому тексті (posted_raw або description_snippet) \
є вказівка на дату/скільки часу тому опубліковано — розрахуй posted_date як \
ISO-дату (сьогоднішня дата вказана в самому кінці цього повідомлення) і \
date_undetermined=false. Якщо вказівки немає взагалі — posted_date=null, \
date_undetermined=true (це не означає відхилення — просто позначка).

Лаконічність: reject_reason, match_reasoning і low_match_location_reason — \
НЕ довше 150 символів кожне (відповідь на десятки вакансій інакше \
обрізається).

Брак окремого інструмента (наприклад, Power BI при наявному Tableau) знижує \
компонент A і Match-рівень, але НЕ є причиною відсіву: passes_criteria \
лишається true.

Поверни СТРОГО валідний JSON (без markdown-обгортки, без пояснень поза JSON) \
за схемою:
{{
  "evaluations": [
    {{
      "raw_index": <int, індекс з вхідного списку>,
      "passes_criteria": <bool>,
      "reject_reason": <string або null>,
      "reject_code": <null якщо passes_criteria=true, інакше ОДИН з: \
"роль"|"досвід"|"формат"|"ЗП"|"англійська"|"військова служба"|"інше" — перший критерій, \
на якому вакансію відхилено>,
      "normalized_title": <string>,
      "normalized_company": <string, БЕЗ "CV"/"Resume"/"Junior"/назви ролі — \
лише власна назва компанії>,
      "match_level": <"High"|"Medium"|"Low"|null>,
      "match_scores": <{{"skills": 0-2, "duties": 0-2, "expectations": 0-2}} або null>,
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


# Двоетапна оцінка (додано 02.10.2026, спільні правила з онлайн-версією —
# див. docs/ALIGNMENT.md). Етап "card" — грубе сито по картці видачі;
# етап "full" — вирішальна перевірка по повному тексту вакансії. Без цього
# розподілу картка без згадки формату/досвіду "мовчки" проходила, і в звіт
# потрапляли офісні вакансії та вакансії з вимогою 2+ роки.
VACANCY_STAGE_CARD = """ЕТАП 1 з 2 — картка зі списку видачі. Тобі дано лише коротку картку \
(description_snippet), а не повний текст вакансії. Відсіюй (passes_criteria=false) \
ЛИШЕ коли картка ЯВНО суперечить критерію: прямо названо офіс/гібрид без \
варіанту remote, досвід 2+ роки, рівень Middle/Senior/Lead/Head, роль зовсім \
не аналітична (BA/операційну аналітику НЕ відсівай), англійська C1+, ЗП нижче порогу. Якщо інформації \
бракує — passes_criteria=true: вакансію буде перевірено на етапі 2 за \
повним текстом. match_level на цьому етапі попередній."""

VACANCY_STAGE_FULL = """ЕТАП 2 з 2 — ВИРІШАЛЬНИЙ. description_snippet — повний текст вакансії \
(якщо full_text_available=false — повний текст отримати не вдалося, і це лише \
картка). Діють правила ПІДТВЕРДЖЕННЯ (мають пріоритет над загальними вище):
- Формат: passes_criteria=true лише якщо текст ЯВНО дозволяє працювати \
повністю віддалено (remote / віддалено / дистанційно, зокрема «гібрид або \
віддалено» як вибір працівника). Лише офіс, гібрид з обов'язковими днями в \
офісі, релокація — reject_code "формат". Формат не згадано взагалі — теж \
reject_code "формат", reject_reason "формат не підтверджено". Сама лише назва \
міста (наприклад «Київ») НЕ є підтвердженням remote. Якщо в полі зайнятості \
(кластери/вид зайнятості) є «Удаленная работа» / «Віддалена робота» — це \
підтверджений remote.
- Досвід: якщо вимога не вказана взагалі — це допустимо (критерій «без \
вказаного рівня»). Якщо вказана — застосовуй правила досвіду вище. Рівень \
Middle/Senior/Lead/Head/Principal у назві або тексті — reject_code "досвід".
- Роль: аналітика даних/продукту як основна робота. Аналітик-керівник, \
менеджер, розробник, або позиція без жодної аналітики даних — "роль". \
Бізнес-аналітик (BA), операційна (Ops) і системна аналітика: якщо є хоча б \
ОДИН збіг із навичками кандидата (SQL, Excel/Sheets, BI, Python, робота з \
даними) — НЕ відсівається за "роль": passes_criteria=true, Match-рівень Low \
(обов'язки поза профілем). Нуль збігів із навичками кандидата — відсів, \
reject_code "роль" (приклад: "Бізнес-аналітик CRM Dynamics 365" без SQL/Python/ \
BI/Excel і без роботи з даними — відсів).
- Дата публікації: шукай у тексті (поле дати, «N днів тому», дата в описі).
- Повторна звірка ПЕРЕД рішенням: ЗП (поріг із критеріїв вище або не вказана) і дата публікації, знайдені саме на повній сторінці, ще раз звір з \
критеріями (дата — «сьогодні/вчора» за місцевим часом кандидата). Дані повної \
сторінки завжди переважають дані картки зі списку видачі.
Не вигадуй відсутніх фактів: чого немає в тексті — того немає."""


def build_vacancy_eval_parts(
    raw_jobs: list[RawJobPosting],
    today: date | None = None,
    stage: str = "card",
    full_texts: dict[int, str] | None = None,
) -> tuple[str, str]:
    """(статична частина, динамічна частина) промпту. Статична — профіль
    кандидата + правила + блок етапу — однакова для всіх батчів етапу, тож
    йде в prompt caching (client.call_json(cache_prefix=...)). Динамічна —
    дата й вакансії батча. static + dynamic == build_vacancy_eval_prompt().
    stage="card" — етап 1 (картка), stage="full" — етап 2 (повний текст).
    full_texts: {індекс у raw_jobs: повний текст} для етапу 2; для вакансій
    без повного тексту передається картка з full_text_available=false."""
    today = today or date.today()
    full_texts = full_texts or {}
    stage_block = VACANCY_STAGE_FULL if stage == "full" else VACANCY_STAGE_CARD
    payload = [
        {
            "raw_index": i,
            "source": job.source,
            "title": job.title,
            "company": job.company,
            "url": job.url,
            "posted_raw": job.posted_raw,
            "salary_raw": job.salary_raw,
            "description_snippet": full_texts.get(i, job.description_snippet),
            **({"full_text_available": i in full_texts} if stage == "full" else {}),
        }
        for i, job in enumerate(raw_jobs)
    ]
    # "Сьогодні" підставляється рівно один раз, простим конкатом (як і в
    # build_email_classify_prompt) — без плейсхолдера всередині константи
    # VACANCY_EVAL_SYSTEM_PROMPT, щоб не тримати f-string-екранування
    # ({{ }}) і .replace() як дві паралельні механіки для того самого.
    static = VACANCY_EVAL_SYSTEM_PROMPT + "\n\n" + stage_block + "\n\n"
    dynamic = "Сьогоднішня дата: " + today.isoformat() + "\n\nВакансії:\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    return static, dynamic


def build_vacancy_eval_prompt(
    raw_jobs: list[RawJobPosting],
    today: date | None = None,
    stage: str = "card",
    full_texts: dict[int, str] | None = None,
) -> str:
    """Той самий промпт одним рядком (без розбиття для кешу)."""
    static, dynamic = build_vacancy_eval_parts(raw_jobs, today, stage, full_texts)
    return static + dynamic


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
(сьогоднішня дата вказана в самому кінці цього повідомлення), інакше null \
з date_undetermined=true.
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
    return FREELANCE_EVAL_SYSTEM_PROMPT + "\n\nСьогоднішня дата: " + today.isoformat() + "\n\nПроєкти/пости:\n" + json.dumps(
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


def build_email_classify_parts(raw_emails: list[dict]) -> tuple[str, str]:
    """(статична частина, динамічна) — для prompt caching; разом дають
    build_email_classify_prompt()."""
    full = build_email_classify_prompt(raw_emails)
    static = EMAIL_CLASSIFY_SYSTEM_PROMPT + "\n\n"
    return static, full[len(static):]

"""Витяг структурованих полів з постів через Claude. Зберігаються лише поля,
тексти постів і контакти рекрутерів — ні."""
from __future__ import annotations

import json

DIRECTIONS = ["analytics", "ai-ml", "devops", "qa", "pm", "dev", "design", "marketing", "other-it", "non-it"]
LEVELS = ["intern", "junior", "middle", "senior", "lead", "unspecified"]
FORMATS = ["remote", "hybrid", "office", "unspecified"]

PROMPT = """Ти розбираєш пости Telegram-каналів з вакансіями. Для КОЖНОГО поста зі списку поверни об'єкт.

Поля поста: key (як у вхідних даних), is_vacancy (true, якщо пост пропонує хоча б одну вакансію/стажування),
vacancies — список вакансій у пості (порожній, якщо не вакансія). Для кожної вакансії:
- title: назва посади, як у пості
- company: роботодавець або null (НЕ контакт рекрутера)
- is_it: чи це IT/tech-роль
- direction: один з {directions}. Правила (застосовуй однаково в усіх постах):
  analytics = data/BI/product/business/system analyst, Power BI/BI developer, data engineer;
  ai-ml = ML/DS/AI engineer, AI developer; pm = project/product manager, product owner, scrum master, PM assistant;
  dev = розробка ПЗ, embedded/hardware engineer, database developer; qa = тестування;
  devops = DevOps/SRE/cloud/CI-CD, system integration; design = UI/UX/графічний дизайн;
  marketing = SMM, digital/performance marketing, CRM-маркетинг, контент, farmer (is_it=false);
  other-it = tech support, sysadmin, security, IT-аудит, адміністратор сайту;
  non-it = продажі, фінанси, HR, рекрутинг/sourcing, освіта, операції (is_it=false)
- level: один з {levels}. unspecified, якщо рівень не вказано явно і його не видно з вимог досвіду
- experience_years_min: мінімальний досвід у роках або null
- skills: інструменти й технології, короткі нормалізовані назви (SQL, Python, Power BI, Excel, Tableau, AWS, Kubernetes…), без soft skills
- work_format: один з {formats}
- remote_scope: ukraine, worldwide, eu або null
- salary_stated: true/false; salary_min, salary_max: числа або null; salary_currency: USD/UAH/EUR або null;
  salary_period: month, hour, project або null
- link: посилання на оголошення з рядка «Посилання» (сайт компанії, job-борд, скорочене посилання), якщо воно стосується саме цієї вакансії, інакше null
Якщо пост — дайджест кількох вакансій, перелічи кожну окремо з тими самими правилами.

Поверни СТРОГО JSON без markdown: {{"posts": [{{"key": ..., "is_vacancy": ..., "vacancies": [...]}}]}}

Пости:
{payload}"""


def build_prompt(posts) -> str:
    payload = [{"key": f"{p.channel}/{p.post_id}", "text": p.text[:1500]} for p in posts]
    return PROMPT.format(
        directions=", ".join(DIRECTIONS), levels=", ".join(LEVELS), formats=", ".join(FORMATS),
        payload=json.dumps(payload, ensure_ascii=False),
    )


def _norm(value, allowed, default):
    v = (value or "").strip().lower() if isinstance(value, str) else value
    return v if v in allowed else default


def normalize_vacancy(v: dict) -> dict:
    return {
        "title": (v.get("title") or "").strip(),
        "company": (v.get("company") or "").strip(),
        "is_it": bool(v.get("is_it")),
        "direction": _norm(v.get("direction"), DIRECTIONS, "other-it" if v.get("is_it") else "non-it"),
        "level": _norm(v.get("level"), LEVELS, "unspecified"),
        "experience_years_min": v.get("experience_years_min"),
        "skills": "; ".join(s.strip() for s in (v.get("skills") or []) if isinstance(s, str) and s.strip()),
        "work_format": _norm(v.get("work_format"), FORMATS, "unspecified"),
        "remote_scope": v.get("remote_scope") or "",
        "salary_stated": bool(v.get("salary_stated")),
        "salary_min": v.get("salary_min"),
        "salary_max": v.get("salary_max"),
        "salary_currency": v.get("salary_currency") or "",
        "salary_period": _norm(v.get("salary_period"), ["month", "hour", "project"], ""),
        "link": v.get("link") or "",
    }

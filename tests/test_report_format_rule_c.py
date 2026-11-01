"""Правило Ц (docs/ALIGNMENT.md): формат фінального виводу. Лише формат —
дані й фільтри не чіпаються."""
from __future__ import annotations

import importlib
import re
from types import SimpleNamespace as NS

from config import CATEGORY_BLOCK_TITLE, CATEGORY_NONE, GMAIL_LABEL_NAME
from models import SourceStatus
from pipeline.step4_selfcheck import format_selfcheck_block, selfcheck_notes

main = importlib.import_module("main")


def _vac(i=0, category=CATEGORY_NONE, **kw):
    base = dict(title=f"Data Analyst {i}", company=f"Company {i}", source="djinni.co",
                url=f"https://djinni.co/jobs/{i}", match_level="Medium",
                match_reasoning="SQL і Python збігаються, Tableau бракує.", flags=[], card_only=False,
                company_flag="", low_match_location=False, low_match_location_reason="",
                date_undetermined=False, posted_date="2026-11-02", veteran_bonus=False,
                disability=False, category=category)
    base.update(kw)
    return NS(**base)


def _selfcheck():
    return NS(timestamp_utc="2026-11-02 10:00 UTC", step1_status="OK", step1_note="", step2_status="OK", step2_note="",
              step3_metrics_status="OK", step3_metrics_note="", step3_tracker_status="OK", step3_tracker_note="без нових відмов",
              sources_ok_count=5, collection_method="перша_сторінка", conversion_pct="3", trend_comparable=True,
              trend_comparable_reason="", step1_2_status="OK", step1_2_note="", shadow_summary="тінь вимкнено",
              notes="Вартість Claude: $0.250", low_match_streak_signal=True)


def _report(vacancies=(), projects=(), findings=(), **kw):
    statuses = {n: SourceStatus(n, "OK") for n in ["djinni.co", "jobs.dou.ua", "robota.ua", "work.ua", "happymonday.ua"]}
    step1 = {"vacancies": list(vacancies), "near_misses": [], "source_statuses": statuses,
             "dedup_log_note": "службова нотатка дедуплікації", "unknown_date_note": ""}
    step1_2 = {"projects": list(projects), "source_statuses": {}, "dedup_log_note": ""}
    step2 = {"findings": list(findings), "notes": ["нотатка пошти"]}
    step4 = {"result": _selfcheck(), "saved": True}
    return main.format_report(step1, step1_2, step2, {"saved": True}, step4, **kw)


def test_blocks_are_bold_headings_in_rule_c_order_with_notes_last():
    text = _report([_vac()])
    assert "## " not in text
    titles = ["**Крок 1 — нові вакансії**", CATEGORY_BLOCK_TITLE, "**Майже підійшли**",
              "**Крок 1.2 — фріланс**", "**Крок 2 — пошта**", "**Крок 3 — без відповіді понад місяць**",
              "**Крок 4 — самоперевірка**", "**Примітки**"]
    positions = [text.index(t) for t in titles]
    assert positions == sorted(positions)
    assert text.rstrip().splitlines()[-1].startswith("- ") and text.index("**Примітки**") > text.index("**Крок 4")


def test_main_vacancy_item_format():
    text = _report([_vac(0)])
    assert "- **Data Analyst 0** | Company 0 | [djinni.co](https://djinni.co/jobs/0)" in text
    block = text.split("**Крок 1 — нові вакансії**")[1].split(CATEGORY_BLOCK_TITLE)[0]
    labels = re.findall(r"^  - ([^:]+):", block, flags=re.M)
    assert labels == ["Match-рівень", "Оцінка відповідності", "Ветеранські бонуси", "Обмеження локації", "Дата публікації"]
    # Правило Ц п.6: при «ні» — просто «ні», без вигаданої причини
    assert "  - Ветеранські бонуси: ні\n" in block and "  - Обмеження локації: ні\n" in block


def test_location_restriction_explained_and_flags_inside_assessment():
    text = _report([_vac(0, low_match_location=True, low_match_location_reason="лише для тих, хто в Україні",
                         flags=["лише картка"], company_flag="Компанія вже в таблиці: відмова")])
    assert "Обмеження локації: так (лише для тих, хто в Україні)" in text
    assert "Оцінка відповідності: SQL і Python збігаються, Tableau бракує. [лише картка; Компанія вже в таблиці: відмова]" in text
    assert "Прапорець:" not in text


def test_veteran_section_keeps_item_format_with_category_and_goes_before_near_misses():
    text = _report([_vac(0), _vac(1, category="ветеран", veteran_bonus=True)])
    section = text.split(f"**{CATEGORY_BLOCK_TITLE}**")[1].split("**Майже підійшли**")[0]
    assert "Категорія: ветеран" in section and "Інвалідність:" in section and "Ветерани: так" in section
    assert "Data Analyst 1" in section and "Data Analyst 0" not in section


def test_empty_category_block_and_sources_line_names_all_five():
    text = _report([])
    assert f"{CATEGORY_BLOCK_TITLE}: немає" in text
    line = next(l for l in text.splitlines() if l.startswith("Статуси джерел:"))
    for name in ["djinni.co", "jobs.dou.ua", "robota.ua", "work.ua", "happymonday.ua"]:
        assert name in line


def test_step_1_2_is_compact_one_line_per_project():
    p = NS(source="freelancehunt.com", title="Дашборд", category="BI и аналитика данных", url="https://fh/1",
           competition_level="середня", bids_count=20, budget_raw="", posted_date="2026-11-02", why_relevant="BI-дашборд")
    block = _report(projects=[p]).split("**Крок 1.2 — фріланс**")[1].split("**Крок 2")[0]
    assert len([l for l in block.splitlines() if l.strip()]) == 1
    assert "[Freelancehunt](https://fh/1)" in block
    empty = _report().split("**Крок 1.2 — фріланс**")[1].split("**Крок 2")[0]
    assert "не знайдено" in empty


def test_step_2_numbered_list_with_status_and_label_line():
    f = [NS(sender="hr@acme.com", subject="Співбесіда", date="02 Nov", summary="Запрошення на співбесіду", status="запрошення на співбесіду"),
         NS(sender="jobs@x.com", subject="Відмова", date="02 Nov", summary="Відмова", status=None)]
    block = _report(findings=f).split("**Крок 2 — пошта**")[1].split("**Крок 3")[0]
    assert "1. hr@acme.com | Співбесіда | 02 Nov | Запрошення на співбесіду" in block
    assert "Статус: запрошення на співбесіду" in block and "2. jobs@x.com" in block
    assert f"Лейбл «{GMAIL_LABEL_NAME}» додано до листів: 2." in block


def test_step_3_block_is_present_and_metric_status_goes_to_notes():
    text = _report()
    block = text.split("**Крок 3 — без відповіді понад місяць**")[1].split("**Крок 4")[0]
    assert "лише онлайн" in block and "Метрику" not in block
    assert "Метрику збережено" in text.split("**Примітки**")[1]


def test_step_4_compact_list_and_service_lines_only_in_notes():
    text = _report()
    step4 = text.split("**Крок 4 — самоперевірка**")[1].split("**Примітки**")[0]
    assert re.search(r"^- Крок 1: OK", step4, flags=re.M) and "Джерела: 5/5" in step4 and "Конверсія" in step4
    assert "Вартість" not in step4 and "тінь" not in step4 and "Сигнал" not in step4 and "Перевірка запуску" not in step4
    notes = text.split("**Примітки**")[1]
    for needle in ["службова нотатка дедуплікації", "нотатка пошти", "Вартість Claude: $0.250", "Сигнал (евристика)", "тінь вимкнено"]:
        assert needle in notes


def test_alarms_and_verified_line_stay_on_top_outside_blocks():
    text = _report(alarms=["🚨 НЕ ЗАПИСАНО: лог"], verified_ok=False)
    assert text.index("НЕ ЗАПИСАНО") < text.index("**Крок 1 — нові вакансії**")
    ok = _report(verified_ok=True)
    assert ok.startswith(main.VERIFIED_OK_LINE)


def test_selfcheck_helpers_split_core_and_notes():
    r = _selfcheck()
    assert "Вартість" not in format_selfcheck_block(r)
    assert any("Вартість" in n for n in selfcheck_notes(r))


def test_explanation_required_only_for_yes_rule_c_p6():
    text = _report([_vac(0, low_match_location=True, low_match_location_reason="лише для тих, хто в Україні")])
    assert "  - Обмеження локації: так (лише для тих, хто в Україні)\n" in text
    assert "  - Ветеранські бонуси: ні\n" in text
    yes = _report([_vac(0, veteran_bonus=True)])
    assert "  - Ветеранські бонуси: так (" in yes

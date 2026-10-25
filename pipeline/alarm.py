"""Тривога (26.10.2026): збій скрейпера (403, помилка, 0 карток там, де зазвичай
десятки, зміна верстки), невдалий запис логу/метрик у Drive або аварія кроку.

Причини збираються в `_alarms` протягом запуску й виводяться у ПЕРШОМУ повідомленні —
першими рядками звіту в консолі та на початку тексту push (ntfy) — у форматі
«🚨🚨🚨 **!!! ТЕКСТ ВЕЛИКИМИ ЛІТЕРАМИ !!!**». Тривога = ненульовий exit code (main.py)."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_alarms: list[str] = []  # готові рядки тривог
_MAX_REASON_LEN = 220
NOT_WRITTEN_PREFIX = "НЕ ЗАПИСАНО: "


def reset() -> None:
    _alarms.clear()


def add(reason: str, *, first: bool = False) -> None:
    """Додає тривогу `🚨🚨🚨 **!!! ПРИЧИНА !!!**`; first=True — ставить її першим рядком."""
    reason = " ".join(str(reason).split())[:_MAX_REASON_LEN]
    _add_line(format_line(reason), reason, first)


def add_not_written(what: str) -> None:
    """Тривога верифікації запису: `🚨🚨🚨 !!! НЕ ЗАПИСАНО: <що саме> !!!` — ПЕРШИМ рядком."""
    what = " ".join(str(what).split())[:_MAX_REASON_LEN]
    _add_line(f"🚨🚨🚨 !!! {NOT_WRITTEN_PREFIX}{what.upper()} !!!", what, first=True)


def _add_line(line: str, reason: str, first: bool) -> None:
    if reason and line not in _alarms:
        _alarms.insert(0, line) if first else _alarms.append(line)
        logger.error("ТРИВОГА: %s", line)


def lines() -> list[str]:
    """Готові рядки тривог (для звіту)."""
    return list(_alarms)


def active() -> bool:
    return bool(_alarms)


def format_line(reason: str) -> str:
    return f"🚨🚨🚨 **!!! {reason.strip().upper()} !!!**"


def format_block(items: list[str] | None = None) -> str:
    """Готові рядки тривог по одному в рядок; порожній рядок, якщо тривог немає."""
    return "\n".join(_alarms if items is None else items)


def collect_from_results(step1: dict, step1_2: dict | None, step2: dict | None,
                         step3: dict | None, step4: dict | None) -> None:
    """Тривоги з результатів кроків (джерела, Claude, запис у Drive). Збої, що
    виникають по ходу (запис логу, критичні виключення), main додає через add()."""
    for name, st in (step1.get("source_statuses") or {}).items():
        if st.status == "недоступне":
            add(f"джерело {name} недоступне: {st.note or 'без деталей'}")
        if getattr(st, "anomaly", ""):
            add(f"джерело {name}: {st.anomaly}")
    if step1.get("unevaluated_count"):
        add(f"оцінка Claude не виконана для {step1['unevaluated_count']} вакансій (помилка Claude)")
    if str(step1.get("dedup_log_note", "")).startswith("лог дублів не вдалось оновити"):
        add(f"лог показаних вакансій у Drive не оновлено: {step1['dedup_log_note']}")
    for name, st in ((step1_2 or {}).get("source_statuses") or {}).items():
        if st.status == "недоступне":
            add(f"джерело фрілансу {name} недоступне: {st.note or 'без деталей'}")
    if step1_2 and step1_2.get("metrics_saved") is False:
        add(f"метрику фрілансу не записано в Drive: {step1_2.get('metrics_error', '')}")
    if step2 and step2.get("error"):
        add(f"крок 2 (пошта): {step2['error']}")
    if step3 is not None and not step3.get("saved"):
        add(f"метрику не записано в Drive: {step3.get('error', '')}")
    if step4 is not None and not step4.get("saved"):
        add(f"результат перевірки не записано в Drive: {step4.get('error', '')}")

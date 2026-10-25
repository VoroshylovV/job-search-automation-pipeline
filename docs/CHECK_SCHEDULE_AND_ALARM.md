# Як перевірити розклад і тривогу за 5 хвилин

Нічого не ламає: пайплайн не запускається, Drive, Claude і скрейпери не чіпаються.
Команди — з кореня репо (на Windows — у PowerShell з активованим `venv`).

## 1. Тривога: імітація збою (≈1 хв)

```bash
python main.py --simulate-fail=metrics
echo $?            # PowerShell: $LASTEXITCODE  -> 2
```

Ключ `<log|shown|metrics|check|sources>` — яку перевірку `verify_writes()` «провалити»:
`log` — лог відсіву, `shown` — Лог показаних, `metrics` — метрики, `check` — «Результат щоденної перевірки»,
`sources` — 5 джерел. Очікуєте:

- у консолі **першим рядком** `🚨🚨🚨 !!! НЕ ЗАПИСАНО: МЕТРИКИ: СИМУЛЯЦІЯ ЗБОЮ … !!!`;
- push у ntfy (тема `PUSH_NTFY_TOPIC`, пріоритет urgent) з тим самим рядком першим;
- код виходу **2**.

Прапорець працює лише з ручним запуском: з `--scheduled` дає код 64 і повідомлення про помилку.
Без `PUSH_NTFY_TOPIC` push пропускається з попередженням — код виходу все одно 2.

## 2. Розклад: рішення «запуск потрібен / не потрібен» (≈1 хв)

```bash
python main.py --scheduled --check-only    # код виходу 0, пайплайн не запускається
```

Друкує час у Варшаві, час UTC і рішення, напр.:
`Час: 2026-10-26 11:59 Europe/Warsaw (UTC 10:59); розклад: 12:00 Europe/Warsaw.` /
`ЗАПУСК НЕ ПОТРІБЕН, бо … ще раніше за 12:00.` Стан «сьогодні вже запускали» не змінюється
(`logs/last_scheduled_run.txt` пише лише справжній `--scheduled`-запуск).

## 3. Task Scheduler на Windows (≈3 хв)

1. Зареєструвати основну задачу (будить кожні 15 хв; запуск — за Europe/Warsaw, не за поясом ПК):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1
   ```

2. Зареєструвати тестову одноразову задачу, що виконує лише `--scheduled --check-only`:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1 -CheckOnly
   Start-ScheduledTask -TaskName JobSearchPipeline-CheckOnly
   ```

3. Прочитати рішення розкладу, яке задача записала в лог, і код завершення:

   ```powershell
   Get-Content logs\scheduler.log -Tail 6
   (Get-ScheduledTaskInfo -TaskName JobSearchPipeline-CheckOnly).LastTaskResult   # 0
   ```

   У хвості логу має бути «Час: … Europe/Warsaw …» і «ЗАПУСК ПОТРІБЕН/НЕ ПОТРІБЕН, бо …».
   Якщо рядків немає або `LastTaskResult` ≠ 0 — шлях до `venv`, права або `wscript` (див. `logs\scheduler.log`).
4. (Необов'язково) прибрати тестову задачу: `Unregister-ScheduledTask -TaskName JobSearchPipeline-CheckOnly -Confirm:$false`.

## Коди виходу

| Код | Значення |
|---|---|
| 0 | штатно / запуск не потрібен / `--check-only` |
| 2 | тривога (або симуляція) |
| 1 | аварія поза кроками |
| 64 | неправильні прапорці |

Деталі тривог, локальних логів і `verify_writes()` — у [README](../README.md#тривога-локальні-логи-повторні-запуски-з-26102026).

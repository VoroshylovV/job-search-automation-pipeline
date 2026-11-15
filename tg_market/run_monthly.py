"""Щомісячний цикл Telegram-ринку одним запуском: кроки пов'язані журналом і продовжуються після збою.

    python -m tg_market.run_monthly                   # цикл за попередній календарний місяць
    python -m tg_market.run_monthly --status          # показати журнал кроків, нічого не запускати
    python -m tg_market.run_monthly --accept-partial  # іти далі, хоча збір частковий (прогалина лишиться в журналі)
    python -m tg_market.run_monthly --restart         # почати цикл заново (зібрані дані не зачіпає)

Ланцюг: preflight → collect → analyze → compare → monthly → drive_export → підсумок.

  * Кожен крок — окремий процес: падіння кроку не валить ланцюг, код завершення і вивід лишаються в журналі
    tg_market/data/monthly_run.json та в tg_market/data/monthly_logs/<цикл>_<крок>.log.
  * Крок запускається лише тоді, коли попередній має слід «ok» (або «accepted»). Після кожного кроку
    перевіряється його результат (код завершення + свіжий файл, який він мав створити).
  * Повторний запуск тієї самої команди продовжує з кроку, що не завершився; виконані кроки не повторюються.
    Збір можна повторювати безпечно: state.json не дає брати оброблені пости вдруге.
  * preflight виконується щоразу і нічого не коштує, крім одного мінімального запиту до API (перевірка ключа й балансу).
  * Підсумок друкується завжди — навіть якщо ланцюг обірвався: де саме, чому і якою командою продовжити.

Ключ API. Збір Telegram іде на окремому ключі «telegram», щоб його витрати не змішувались із щоденним пайплайном.
Ключ задається в терміналі перед запуском (у .env він не зберігається):

    read -s -p "Ключ telegram: " ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY

Якщо в терміналі ключа немає або він збігається з ключем із .env — preflight зупиняє цикл.
"""
from __future__ import annotations

import os

# Ключ із терміналу фіксуємо ДО будь-якого імпорту config.py: той підтягує .env, і джерело ключа вже не розрізнити.
_SHELL_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

import argparse  # noqa: E402
import csv  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from calendar import monthrange  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from tg_market import runlog  # noqa: E402
from tg_market.analyze import DATA, OUT  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SINCE = "2026-08-01"          # сталий початок збору (README: «Ритм»); state.json не дає брати старе вдруге
BRANCH = "main"               # гілка, на якій живе актуальний tg_market
STEPS = ["collect", "analyze", "compare", "monthly", "drive_export"]
DONE = {"ok", "accepted"}
GIT_PATHS = ["tg_market", "claude_orchestrator", "scrapers", "config.py"]
TITLES = {"preflight": "перевірка перед запуском", "collect": "збір і класифікація постів",
          "analyze": "дедуплікація і метрики", "compare": "порівняння Telegram із сайтами",
          "monthly": "помісячний звіт", "drive_export": "вивантаження на Google Drive"}


# ----------------------------------------------------------------------------- дрібні помічники
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def previous_month(today: date) -> str:
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{y}-{m:02d}"


def month_end(month: str) -> date:
    y, m = map(int, month.split("-"))
    return date(y, m, monthrange(y, m)[1])


def say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:          # консоль Windows без UTF-8
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def env_file_values() -> dict:
    path = REPO / ".env"
    if not path.exists():
        return {}
    from dotenv import dotenv_values
    return {k: (v or "").strip() for k, v in dotenv_values(path).items()}


# ----------------------------------------------------------------------------- журнал
def journal_path() -> Path:
    return DATA / "monthly_run.json"


def load_journal(cycle: str, restart: bool = False, readonly: bool = False) -> dict:
    """readonly (--status): нічого не пише і не переносить, навіть якщо на диску журнал іншого циклу."""
    path = journal_path()
    if path.exists():
        try:
            j = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            j = None
        if j and j.get("cycle") == cycle and not restart:
            return j
        if j and not readonly:   # інший цикл або --restart: старий журнал не губимо
            logs = DATA / "monthly_logs"
            logs.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path.replace(logs / f"journal_{j.get('cycle', 'unknown')}_{stamp}.json")
    return {"cycle": cycle, "created_at": now(), "steps": {}}


def save_journal(j: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = journal_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(journal_path())          # запис атомарний: обрив не лишає напівзаписаний журнал


def mark(j: dict, step: str, status: str, note: str = "", **extra) -> None:
    j["steps"][step] = {**j["steps"].get(step, {}), "status": status, "note": note, "at": now(), **extra}
    save_journal(j)


# ----------------------------------------------------------------------------- preflight
def api_ping(model: str, key: str) -> str | None:
    """Мінімальний запит: перевіряє, що ключ дійсний і на балансі є кошти. Повертає текст помилки або None."""
    try:
        import anthropic
        anthropic.Anthropic(api_key=key).messages.create(
            model=model, max_tokens=1, messages=[{"role": "user", "content": "ok"}])
    except Exception as exc:  # noqa: BLE001 — будь-яка відмова API тут означає «не починати збір»
        return f"{type(exc).__name__}: {exc}"
    return None


def preflight(args) -> tuple[list[str], list[str]]:
    """Повертає (помилки, попередження). Помилки зупиняють цикл до будь-яких витрат."""
    errors, warnings = [], []
    env_file = env_file_values()

    # 1. Гілка і стан коду
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        errors.append("git недоступний або це не репозиторій: запускай із папки job-search-automation-pipeline")
    elif branch != args.branch:
        errors.append(f"увімкнена гілка «{branch}», а збір Telegram іде з «{args.branch}»: "
                      f"git checkout {args.branch} && git pull (після збору повернись: git checkout -)")
    else:
        dirty = git("status", "--porcelain", "--", *GIT_PATHS)
        if dirty and not args.allow_dirty:
            errors.append("у коді збору є незакомічені зміни (git status): закоміть чи відкинь їх, "
                          "або запусти з --allow-dirty, якщо так задумано")
        if git("fetch", "--quiet", "origin", args.branch) is None:
            warnings.append("не вдалося звіритись із GitHub (git fetch): чи актуальний код, не перевірено")
        else:
            behind = git("rev-list", "--count", f"HEAD..origin/{args.branch}")
            if behind and behind != "0":
                errors.append(f"локальна гілка відстає від GitHub на {behind} комітів: git pull")

    # 2. Ключ API: окремий «telegram», заданий у терміналі
    file_key = env_file.get("ANTHROPIC_API_KEY", "")
    if args.use_env_key:
        if not (_SHELL_KEY or file_key):
            errors.append("ANTHROPIC_API_KEY не задано ні в терміналі, ні в .env")
        else:
            warnings.append("збір піде на ключі з .env (--use-env-key): витрати змішаються зі щоденним пайплайном")
    elif not _SHELL_KEY:
        errors.append('ключ «telegram» не задано в терміналі. Виконай: read -s -p "Ключ telegram: " ANTHROPIC_API_KEY '
                      "&& export ANTHROPIC_API_KEY — і запусти цикл ще раз у тому самому вікні")
    elif file_key and _SHELL_KEY == file_key:
        errors.append("ключ у терміналі збігається з ключем із .env — це ключ щоденного пайплайна, а не «telegram»")
    else:
        say(f"  ключ API: …{_SHELL_KEY[-4:]} (заданий у терміналі, не з .env)")

    # 3. Дані: шапки CSV без невідомих колонок, позначка обірваного запису читається
    for name, fields in (("posts.csv", runlog.POST_FIELDS), ("vacancies.csv", runlog.VAC_FIELDS),
                         ("runs.csv", runlog.RUN_FIELDS)):
        path = DATA / name
        if path.exists() and path.stat().st_size:
            with open(path, encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
            extra = [c for c in header if c not in fields]
            if extra:
                errors.append(f"{name}: невідомі колонки {extra} — файл записано іншою версією коду, перевір вручну")
    pending = DATA / runlog.PENDING_FILE
    if pending.exists():
        warnings.append("попередній збір обірвався під час запису каналу: крок collect прибере недописані рядки "
                        "(копії .bak) і збере канал заново")

    # 4. Логи сайтів для порівняння і папка Drive
    if not (REPO / "logs").is_dir():
        errors.append(f"немає каталогу {REPO / 'logs'}: крок compare порівнює Telegram із логами щоденного пайплайна")
    if not (os.environ.get("TG_MARKET_DRIVE_FOLDER_ID", "").strip() or env_file.get("TG_MARKET_DRIVE_FOLDER_ID")):
        warnings.append("TG_MARKET_DRIVE_FOLDER_ID не задано в .env: крок drive_export буде заблоковано "
                        "(усе інше виконається, результати лишаться на диску)")

    # 5. Ключ і баланс — останнім, лише якщо все інше гаразд (це єдина перевірка, що йде в мережу Anthropic)
    if not errors and not args.skip_ping:
        model = os.environ.get("TG_MARKET_MODEL") or env_file.get("TG_MARKET_MODEL") or "claude-haiku-5-5"
        problem = api_ping(model, _SHELL_KEY or file_key)
        if problem:
            errors.append(f"API не відповідає на тестовий запит ({problem}). Якщо причина в балансі — поповни "
                          "Console і запусти цикл ще раз")
    return errors, warnings


# ----------------------------------------------------------------------------- виконання кроків
def run_command(cmd: list[str], log_path: Path) -> int:
    """Запускає крок окремим процесом, дублює вивід на екран і в лог. Повертає код завершення."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== {now()} {' '.join(cmd)}\n")
        proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=env)
        try:
            for line in proc.stdout:
                log.write(line)
                say("    " + line.rstrip("\n"))
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            log.write("\n===== перервано користувачем\n")
            raise


def fresh(path: Path, since_ts: float) -> bool:
    return path.exists() and path.stat().st_size > 0 and path.stat().st_mtime >= since_ts - 2


def commands(args, cycle: str) -> dict[str, list[str]]:
    py = sys.executable
    return {
        "collect": [py, "-m", "tg_market.collect", "--since", args.since, "--panel", args.panel],
        "analyze": [py, "-m", "tg_market.analyze"],
        "compare": [py, "-m", "tg_market.compare", "--month", cycle, "--logs", str(REPO / "logs")],
        "monthly": [py, "-m", "tg_market.monthly", "--since", args.since, "--until", month_end(cycle).isoformat(),
                    "--logs", str(REPO / "logs")],
        "drive_export": [py, "-m", "tg_market.drive_export", "--month", cycle],
    }


def judge(step: str, code: int, started_ts: float) -> tuple[str, str]:
    """(статус, примітка) за кодом завершення і слідом кроку."""
    if step == "collect":
        last = runlog.last_run(DATA / "runs.csv") or {}
        problems = last.get("problem_channels", "")
        if code == 0:
            return "ok", ""
        if code == 2:
            return "partial", problems or "є проблемні канали (див. лог кроку)"
        if code == 3:
            return "failed", ("зупинено за лімітом вартості TG_MARKET_BUDGET_USD: зібрані канали збережено; "
                              "перевір обсяг у лозі, за потреби підніми ліміт і запусти цикл ще раз")
        return "failed", f"код завершення {code}: зібрані до збою канали збережено, повторний запуск продовжить"
    if code != 0:
        if step == "drive_export" and code == 2:
            return "blocked", "TG_MARKET_DRIVE_FOLDER_ID не задано в .env — додай і запусти цикл ще раз"
        return "failed", f"код завершення {code} (див. лог кроку)"
    trace = {"analyze": OUT / "unique_vacancies.csv", "compare": OUT / "compare_summary.csv",
             "monthly": OUT / "monthly_tg_vs_repo.csv"}.get(step)
    if trace is not None and not fresh(trace, started_ts):
        return "failed", f"крок завершився без помилки, але не оновив {trace.name} — результату немає"
    return "ok", ""


def run_chain(args, cycle: str, j: dict, runner=run_command) -> None:
    cmds = commands(args, cycle)
    previous_ok = True
    for step in STEPS:
        state = j["steps"].get(step, {})
        status = state.get("status")
        if step == "collect" and status == "partial" and args.accept_partial:
            mark(j, step, "accepted", state.get("note", ""))
            status = "accepted"
            say(f"[{step}] частковий збір прийнято вручну (--accept-partial); прогалина: {state.get('note', '')}")
        if status in DONE:
            say(f"[{step}] уже виконано ({state.get('at', '')}) — пропускаю")
            continue
        if not previous_ok:
            mark(j, step, "not_run", "попередній крок не завершено")
            continue
        say(f"[{step}] {TITLES[step]} …")
        started = datetime.now().timestamp()
        mark(j, step, "running", "", started_at=now())
        log_path = DATA / "monthly_logs" / f"{cycle}_{step}.log"
        try:
            code = runner(cmds[step], log_path)
        except KeyboardInterrupt:
            mark(j, step, "failed", "перервано користувачем", log=str(log_path))
            raise
        except Exception as exc:  # noqa: BLE001 — збій самого запуску процесу теж має лишити слід
            mark(j, step, "failed", f"не вдалося запустити: {type(exc).__name__}: {exc}", log=str(log_path))
            previous_ok = False
            continue
        status, note = judge(step, code, started)
        mark(j, step, status, note, exit_code=code, log=str(log_path))
        say(f"[{step}] {status}" + (f" — {note}" if note else ""))
        previous_ok = status in DONE


def report(j: dict, pre_errors: list[str], pre_warnings: list[str]) -> int:
    say("\n" + "=" * 78)
    say(f"ПІДСУМОК циклу Telegram за {j['cycle']}")
    for w in pre_warnings:
        say(f"  увага: {w}")
    if pre_errors:
        say("  ЦИКЛ НЕ ПОЧАТО — перевірка перед запуском не пройшла:")
        for e in pre_errors:
            say(f"    - {e}")
        say("  Виправ і запусти ту саму команду ще раз. Зібрані раніше дані не змінено.")
        return 1
    stuck = None
    for step in STEPS:
        st = j["steps"].get(step, {})
        status = st.get("status", "not_run")
        say(f"  {step:<13} {status:<9} {st.get('note', '')}")
        if stuck is None and status not in DONE:
            stuck = (step, status)
    if stuck is None:
        say("  Усі кроки виконано. Якщо щоденний прогін іде не з цієї гілки — поверни його гілку: git checkout <назва>")
        return 0
    step, status = stuck
    say(f"  ЛАНЦЮГ ЗУПИНИВСЯ на кроці «{step}» ({status}). Лог: {j['steps'].get(step, {}).get('log', '—')}")
    if step == "collect" and status == "partial":
        say("  Далі: або дозбери проблемні канали — python -m tg_market.collect --since "
            f"{SINCE} --channel <назва> (для «неповного покриття» додай --max-pages, як у примітці), "
            "потім запусти цикл ще раз; або прийми прогалину: python -m tg_market.run_monthly --accept-partial")
    else:
        say("  Далі: усунь причину і запусти ту саму команду ще раз — виконані кроки не повторяться.")
    return 1


def main(argv: list[str] | None = None, runner=run_command) -> int:
    ap = argparse.ArgumentParser(description="Щомісячний цикл Telegram-ринку з журналом кроків")
    ap.add_argument("--month", default=previous_month(date.today()), help="місяць даних YYYY-MM (типово — попередній)")
    ap.add_argument("--since", default=SINCE, help=f"сталий початок збору (типово {SINCE}; не змінюй без потреби)")
    ap.add_argument("--panel", default="core", choices=["core", "secondary", "reference", "all"])
    ap.add_argument("--branch", default=BRANCH, help=f"гілка, з якої дозволено збір (типово {BRANCH})")
    ap.add_argument("--status", action="store_true", help="лише показати журнал кроків")
    ap.add_argument("--restart", action="store_true", help="почати цикл заново (зібрані дані не зачіпає)")
    ap.add_argument("--accept-partial", action="store_true", help="іти далі після часткового збору")
    ap.add_argument("--allow-dirty", action="store_true", help="дозволити незакомічені зміни в коді збору")
    ap.add_argument("--use-env-key", action="store_true", help="дозволити ключ із .env (витрати змішаються з пайплайном)")
    ap.add_argument("--skip-ping", action="store_true", help="не робити тестовий запит до API")
    args = ap.parse_args(argv)

    j = load_journal(args.month, restart=args.restart and not args.status, readonly=args.status)
    if args.status:
        return report(j, [], [])
    say(f"Цикл Telegram за {args.month}: перевірка перед запуском …")
    errors, warnings = preflight(args)
    j["preflight"] = {"at": now(), "errors": errors, "warnings": warnings}
    save_journal(j)
    if errors:
        return report(j, errors, warnings)
    try:
        run_chain(args, args.month, j, runner)
    except KeyboardInterrupt:
        say("\nПерервано. Журнал збережено.")
    return report(j, [], warnings)


if __name__ == "__main__":
    sys.exit(main())

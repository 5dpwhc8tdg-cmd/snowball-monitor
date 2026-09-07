#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обёртка для запуска в GitHub Actions (и вообще где угодно без CLI-флагов).

Никаких параметров передавать не нужно. Бот читает настройки из двух файлов
в этой же папке — их правят прямо в браузере на GitHub:

  portfolio.txt  — ссылка на публичный портфель
  chat_id.txt    — chat_id получателя (или TELEGRAM_CHAT_IDS через запятую)

Токен берётся из секретов GitHub (TELEGRAM_BOT_TOKEN), в файлах его нет.

Полезные артефакты для workflow:
  no_change       — файл создан, можно ничего не коммитить
  run_error       — файл создан, была сетевая/иная ошибка цикла
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VALID_TOKEN = None  # чтобы accidental-leak сканеры не ругались на пустую строку


def _read_file(name: str) -> str:
    """Первая «живая» строка файла: пустые и начинающиеся с # игнорируются."""
    p = HERE / name
    if not p.exists():
        return ""
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip().strip('"').strip("'")
        if line and not line.startswith("#") and not line.startswith("//"):
            return line
    return ""


def _looks_like_url(s: str) -> bool:
    return bool(s) and ("snowball-income.com" in s or s.startswith("http"))


def _looks_like_chat_id(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return bool(parts) and all(p.lstrip("-").isdigit() for p in parts)


def build_args() -> tuple[list[str], list[str], Path]:
    """Возвращает (аргументы watcher.py, причины для пропуска, путь к state.json)."""
    url = os.environ.get("PORTFOLIO_URL") or _read_file("portfolio.txt")
    chat = (os.environ.get("TELEGRAM_CHAT_IDS") or _read_file("chat_id.txt")
            or os.environ.get("TELEGRAM_CHAT_ID") or "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    problems = []
    if not _looks_like_url(url):
        problems.append("в portfolio.txt нет ссылки на портфель")
    if not token or token.startswith("PUT_"):
        problems.append("не задан секрет TELEGRAM_BOT_TOKEN")
    if problems:
        return [], problems, HERE / "state.json"

    args = [sys.executable, str(HERE / "watcher.py"), "--once",
            "--url", url.strip(), "--token", token.strip()]
    if _looks_like_chat_id(chat):
        args += ["--chat", chat.strip()]
    if os.environ.get("DATA_SOURCE"):
        args += ["--source", os.environ["DATA_SOURCE"].strip()]
    state = Path(os.environ.get("STATE_FILE") or (HERE / "state.json"))
    args += ["--state", str(state)]
    return args, [], state


_RESULT_RE = None  # заполняется лениво, см. _parse_result


def _parse_result(out: str) -> tuple[int | None, str]:
    """Достаёт итог из вывода watcher.py: 'RESULT new=2 error="..."'.
    None — итога нет (значит прогон вообще не дошёл до проверки)."""
    global _RESULT_RE
    if _RESULT_RE is None:
        import re
        _RESULT_RE = re.compile(r"RESULT new=(\d+) error=(.*)$")
    new, err = None, ""
    for line in out.splitlines():
        m = _RESULT_RE.search(line.strip())
        if m:
            new = int(m.group(1))
            err = m.group(2).strip().strip('"')
    return new, err


def _digest(state: Path) -> tuple:
    """Только содержательная часть состояния: число известных сделок, их отпечатки
    и счётчик портфеля. last_check/checks сюда НЕ входят, иначе файл менялся бы
    каждый цикл и мы получили бы ~13 000 пустых коммитов в месяц."""
    import hashlib
    import json
    if not state.exists():
        return ("", 0)
    try:
        d = json.loads(state.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ("corrupt", 0)
    seen = "|".join(sorted(d.get("seen", {})))
    cnt = d.get("meta", {}).get("transactions_count")
    return (hashlib.sha1(seen.encode()).hexdigest(), cnt)


def main() -> int:
    for junk in ("no_change", "run_error"):
        (HERE / junk).unlink(missing_ok=True)

    args, problems, state = build_args()
    if problems:
        print("⛔ Запуск пропущен: " + "; ".join(problems), file=sys.stderr)
        print("Ничего страшного не случилось — просто заполни файлы по инструкции.")
        return 3

    safe = list(args)
    if "--token" in safe:
        safe[safe.index("--token") + 1] = "***"
    print("▶ запуск:", " ".join(safe))

    state_pre_existed = state.exists()
    try:
        r = subprocess.run(args, cwd=str(HERE), timeout=int(os.environ.get("RUN_TIMEOUT", "240")),
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        (HERE / "run_error").write_text("timeout", encoding="utf-8")
        print("⏱ таймаут — пропустил цикл, ничего не коммитим", file=sys.stderr)
        return 0
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip())
    new, err = _parse_result(out)
    if r.returncode != 0 and not err:
        err = f"watcher.py завершился с кодом {r.returncode}"
    if err:
        (HERE / "run_error").write_text(err[:400], encoding="utf-8")
    if not state_pre_existed and not state.exists():
        (HERE / "no_change").write_text("1", encoding="utf-8")
        return 0
    if new is None:
        # Итога нет: прогон упал раньше проверки. Коммитить нечего и незачем.
        (HERE / "no_change").write_text("1", encoding="utf-8")
        print("Итог не получен — коммита не будет.", file=sys.stderr)
        return 0
    if err:
        # Источник сломан — это НЕ «нет новых сделок». Пусть прогон будет красным,
        # иначе поломка молчала бы месяцами.
        (HERE / "no_change").write_text("1", encoding="utf-8")
        print(f"⚠️  Источник не работает: {err}", file=sys.stderr)
        return 1

    if new == 0:
        (HERE / "no_change").write_text("1", encoding="utf-8")
        print("✅ Новых сделок нет — коммит не нужен.")
    else:
        print(f"Новых сделок: {new} — коммитим state.json "
              f"(всего отпечатков: {_count_seen(state)})")
    return 0


def _count_seen(state: Path) -> int:
    import json
    try:
        return len(json.loads(state.read_text(encoding="utf-8")).get("seen", {}))
    except Exception:  # noqa: BLE001
        return -1


if __name__ == "__main__":
    sys.exit(main())

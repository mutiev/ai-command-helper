#!/usr/bin/env python3
"""
ai — CLI-ассистент на базе Claude.
Установка:
  chmod +x ai && mv ai ~/.local/bin/ai
  echo 'sk-ant-...' > ~/.config/ai/api_key   # ваш ключ

Использование:
  ai "как найти файлы больше 100MB?"
  ai -d /etc "покажи конфиги nginx"
  ai -f server.log "почему падает сервис?"
  ai          # интерактивный режим
"""

import os
import sys
import json
import stat
import argparse
import textwrap
import subprocess
from pathlib import Path

# ── Зависимости ─────────────────────────────────────────────────────────────

def ensure_deps():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("Устанавливаю anthropic...", file=sys.stderr)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "anthropic"],
            stdout=subprocess.DEVNULL,
        )

ensure_deps()
import anthropic  # noqa: E402

# ── Конфиг ──────────────────────────────────────────────────────────────────

CONFIG_DIR  = Path.home() / ".config" / "ai"
KEY_FILE    = CONFIG_DIR / "api_key"
SYSTEM_FILE = CONFIG_DIR / "system_prompt"   # необязательный кастомный промпт

MODEL   = "claude-sonnet-4-5"
MAX_TOK = 4096

DEFAULT_SYSTEM = textwrap.dedent("""\
    Ты — ассистент в терминале Linux. Пользователь работает по SSH.
    Правила:
    1. Когда задача решается командой — выдавай ТОЛЬКО команду (или несколько),
       без лишних объяснений, если не просят объяснений.
    2. Обворачивай команды в ```bash``` блоки.
    3. Если нужно посмотреть другие файлы/каталоги — используй инструменты.
    4. Будь лаконичен. Терминал — не место для эссе.
    5. Если задача неоднозначна — сначала уточни, потом предлагай решение.
""")

# ── ANSI-цвета ───────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
DIM    = "\033[2m"

def _tty():
    return sys.stdout.isatty()

def color(text, *codes):
    if not _tty():
        return text
    return "".join(codes) + text + RESET

# ── Утилиты ──────────────────────────────────────────────────────────────────

def read_api_key() -> str:
    # 1. переменная окружения
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    # 2. файл
    if KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
        if key:
            return key
    print(color("✗ API-ключ не найден.", BOLD), file=sys.stderr)
    print(f"  Создайте файл: {KEY_FILE}", file=sys.stderr)
    print( "  Или задайте переменную ANTHROPIC_API_KEY", file=sys.stderr)
    sys.exit(1)

def read_system_prompt() -> str:
    if SYSTEM_FILE.exists():
        return SYSTEM_FILE.read_text().strip()
    return DEFAULT_SYSTEM

def list_dir(path: str, max_entries: int = 200) -> str:
    """Список файлов каталога — для контекста и tool use."""
    p = Path(path).expanduser()
    if not p.exists():
        return f"Каталог не существует: {path}"
    if not p.is_dir():
        return f"Не каталог: {path}"
    try:
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        lines = []
        for e in entries[:max_entries]:
            try:
                s = e.stat()
                kind = "d" if e.is_dir() else ("-" if e.is_file() else "?")
                size = f"{s.st_size:>10}" if e.is_file() else "          "
                lines.append(f"{kind} {size}  {e.name}")
            except PermissionError:
                lines.append(f"? {'':>10}  {e.name}  [нет доступа]")
        if len(entries) > max_entries:
            lines.append(f"... и ещё {len(entries) - max_entries} объектов")
        return "\n".join(lines) if lines else "(каталог пуст)"
    except PermissionError:
        return f"Нет доступа к каталогу: {path}"

def read_file_safe(path: str, max_bytes: int = 32_768) -> str:
    """Читает файл с ограничением размера."""
    p = Path(path).expanduser()
    if not p.exists():
        return f"Файл не существует: {path}"
    if not p.is_file():
        return f"Не файл: {path}"
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            raw = f.read(max_bytes)
        # Проверка на бинарный файл
        if b"\x00" in raw:
            return f"[бинарный файл, {size} байт] — текстовое содержимое недоступно"
        text = raw.decode("utf-8", errors="replace")
        if size > max_bytes:
            text += f"\n\n... [обрезано, показано {max_bytes} из {size} байт]"
        return text
    except PermissionError:
        return f"Нет доступа к файлу: {path}"

def run_shell(cmd: str, timeout: int = 10) -> str:
    """Безопасный запуск команды (read-only по умолчанию)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=os.getcwd(),
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]:\n{err}")
        if r.returncode != 0:
            parts.append(f"[код возврата: {r.returncode}]")
        return "\n".join(parts) if parts else "(нет вывода)"
    except subprocess.TimeoutExpired:
        return f"[таймаут {timeout}с]"
    except Exception as e:
        return f"[ошибка: {e}]"

# ── Инструменты для модели ────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "list_directory",
        "description": (
            "Показывает содержимое каталога файловой системы. "
            "Используй когда нужно понять структуру проекта или найти файлы."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Абсолютный или относительный путь к каталогу",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Читает содержимое текстового файла (до 32 КБ). "
            "Используй для анализа конфигов, логов, скриптов и т.д."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Путь к файлу",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Выполняет shell-команду и возвращает вывод. "
            "Используй только для чтения данных: df, ps, uname, systemctl status и т.п. "
            "НЕ используй для команд, изменяющих систему."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell-команда для выполнения",
                }
            },
            "required": ["command"],
        },
    },
]

def dispatch_tool(name: str, inp: dict) -> str:
    if name == "list_directory":
        return list_dir(inp.get("path", "."))
    elif name == "read_file":
        return read_file_safe(inp.get("path", ""))
    elif name == "run_command":
        cmd = inp.get("command", "")
        print(color(f"  ⚙ {cmd}", DIM), file=sys.stderr)
        return run_shell(cmd)
    return f"Неизвестный инструмент: {name}"

# ── Рендер ответа ────────────────────────────────────────────────────────────

def render(text: str):
    """Простой рендер: подсвечивает ```bash``` блоки."""
    if not _tty():
        print(text)
        return

    lines = text.split("\n")
    in_code = False
    lang = ""
    buf = []

    def flush_code():
        block = "\n".join(buf)
        print(color(block, GREEN, BOLD))
        buf.clear()

    for line in lines:
        if line.startswith("```") and not in_code:
            in_code = True
            lang = line[3:].strip()
            print(color(f"╭─ {lang or 'code'} ", DIM))
            continue
        if line.startswith("```") and in_code:
            flush_code()
            in_code = False
            print(color("╰─────", DIM))
            continue
        if in_code:
            buf.append(line)
        else:
            print(line)

    if buf:
        flush_code()

# ── Основной цикл ─────────────────────────────────────────────────────────────

def ask(client: anthropic.Anthropic, messages: list, system: str) -> str:
    """Отправляет запрос, обрабатывает tool use, возвращает финальный текст."""
    while True:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOK,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # Собираем текст и tool_use из ответа
        text_parts = []
        tool_calls = []

        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)

        final_text = "\n".join(text_parts).strip()

        # Нет вызовов инструментов — возвращаем текст
        if not tool_calls:
            return final_text

        # Добавляем ответ ассистента в историю
        messages.append({"role": "assistant", "content": resp.content})

        # Обрабатываем инструменты
        tool_results = []
        for tc in tool_calls:
            print(color(f"  → {tc.name}({json.dumps(tc.input, ensure_ascii=False)})", DIM + YELLOW), file=sys.stderr)
            result = dispatch_tool(tc.name, tc.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})
        # Продолжаем цикл

# ── Точка входа ───────────────────────────────────────────────────────────────

def build_context(extra_dirs: list, extra_files: list) -> str:
    """Формирует системный контекст: cwd + переданные пути."""
    cwd = Path.cwd()
    parts = [f"## Рабочий каталог: {cwd}"]
    parts.append(list_dir(str(cwd)))

    for d in extra_dirs:
        parts.append(f"\n## Каталог: {d}")
        parts.append(list_dir(d))

    for f in extra_files:
        parts.append(f"\n## Файл: {f}")
        parts.append(read_file_safe(f))

    # Базовая системная информация
    hostname = run_shell("hostname", timeout=3)
    uname    = run_shell("uname -sr", timeout=3)
    parts.append(f"\n## Система: {hostname.strip()} / {uname.strip()}")

    return "\n".join(parts)

def main():
    parser = argparse.ArgumentParser(
        prog="ai",
        description="Claude в терминале",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Примеры:
              ai "как найти файлы больше 100MB?"
              ai -d /etc/nginx "объясни конфиг"
              ai -f error.log "что означает эта ошибка?"
              ai           # интерактивный режим
        """),
    )
    parser.add_argument("query", nargs="?", help="Вопрос (если не указан — интерактивный режим)")
    parser.add_argument("-d", "--dir",  action="append", default=[], metavar="PATH", help="Дополнительный каталог для контекста")
    parser.add_argument("-f", "--file", action="append", default=[], metavar="PATH", help="Дополнительный файл для контекста")
    parser.add_argument("-v", "--verbose", action="store_true", help="Показывать вызовы инструментов")
    parser.add_argument("--setup", action="store_true", help="Интерактивная настройка")
    args = parser.parse_args()

    # ── Первичная настройка ──────────────────────────────────────────────
    if args.setup:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Введите ваш Anthropic API key (sk-ant-...): ", end="", flush=True)
        key = input().strip()
        KEY_FILE.write_text(key)
        KEY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        print(color(f"✓ Ключ сохранён в {KEY_FILE}", GREEN))
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    api_key = read_api_key()
    client  = anthropic.Anthropic(api_key=api_key)
    system  = read_system_prompt()

    ctx = build_context(args.dir, args.file)
    full_system = system + "\n\n" + ctx

    # ── Режим одного вопроса ──────────────────────────────────────────────
    if args.query:
        messages = [{"role": "user", "content": args.query}]
        answer = ask(client, messages, full_system)
        render(answer)
        return

    # ── Интерактивный режим ────────────────────────────────────────────────
    print(color("Claude AI · терминал-ассистент", BOLD + CYAN))
    print(color(f"  Каталог: {Path.cwd()}  |  Модель: {MODEL}", DIM))
    print(color("  Введите вопрос или 'exit' для выхода\n", DIM))

    messages: list = []

    while True:
        try:
            if _tty():
                print(color("❯ ", BOLD + CYAN), end="", flush=True)
            query = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not query:
            continue
        if query.lower() in ("exit", "quit", "q", "выход"):
            break

        messages.append({"role": "user", "content": query})
        answer = ask(client, messages, full_system)
        # Сохраняем финальный ответ в историю
        messages.append({"role": "assistant", "content": answer})

        print()
        render(answer)
        print()


if __name__ == "__main__":
    main()

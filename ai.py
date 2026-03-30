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
import hashlib
import argparse
import textwrap
import subprocess
from datetime import datetime
from pathlib import Path

# ── Зависимости ─────────────────────────────────────────────────────────────

VENV_DIR = Path.home() / ".local" / "share" / "ai" / "venv"

def _reexec_in_venv():
    """Перезапуск через Python из venv (мгновенно, без повторной установки)."""
    venv_python = VENV_DIR / "bin" / "python"
    # sys.prefix == sys.base_prefix → мы НЕ в venv
    if venv_python.is_file() and sys.prefix == sys.base_prefix:
        os.execv(str(venv_python), [str(venv_python)] + sys.argv)

DEPS = ["anthropic", "questionary"]

def ensure_deps():
    try:
        import anthropic  # noqa: F401
        import questionary  # noqa: F401
        return
    except ImportError:
        pass

    # Если venv уже создан — просто перезапуск через него
    if (VENV_DIR / "bin" / "python").is_file():
        _reexec_in_venv()

    # Попытка прямой установки через pip
    try:
        print("Устанавливаю зависимости...", file=sys.stderr)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q"] + DEPS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    except subprocess.CalledProcessError:
        pass

    # PEP 668: создаём venv и перезапускаем себя через него
    print("Создаю виртуальное окружение (PEP 668)...", file=sys.stderr)
    VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    venv_pip = str(VENV_DIR / "bin" / "pip")
    subprocess.check_call(
        [venv_pip, "install", "-q"] + DEPS,
        stdout=subprocess.DEVNULL,
    )
    _reexec_in_venv()

ensure_deps()
import anthropic   # noqa: E402
import questionary  # noqa: E402

# ── Конфиг ──────────────────────────────────────────────────────────────────

CONFIG_DIR   = Path.home() / ".config" / "ai"
KEY_FILE     = CONFIG_DIR / "api_key"
SYSTEM_FILE  = CONFIG_DIR / "system_prompt"   # необязательный кастомный промпт
SESSIONS_DIR = CONFIG_DIR / "sessions"

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
    # 3. интерактивный запрос при первом запуске
    print(color("┌─ Первый запуск: API-ключ не найден ─────────────────────", CYAN))
    print(color("│", CYAN))
    print(color("│", CYAN) + f"  Получить ключ: {CYAN}https://console.anthropic.com/settings/keys{RESET}")
    print(color("│", CYAN))
    if not sys.stdin.isatty():
        print(color("│", CYAN) + color("  ✗ stdin не является терминалом — не могу запросить ключ.", BOLD))
        print(color("│", CYAN) + f"  Сохраните вручную: echo 'sk-ant-...' > {KEY_FILE} && chmod 600 {KEY_FILE}")
        print(color("└──────────────────────────────────────────────────────────", CYAN))
        sys.exit(1)
    try:
        import getpass
        key = getpass.getpass(color("│  ", CYAN) + "Введите API-ключ (ввод скрыт): ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)
    key = key.strip()
    if not key:
        print(color("│  ✗ Пустой ключ — выход.", BOLD))
        print(color("└──────────────────────────────────────────────────────────", CYAN))
        sys.exit(1)
    if not key.startswith("sk-ant-"):
        print(color("│  ✗ Ключ должен начинаться с sk-ant-", BOLD))
        print(color("└──────────────────────────────────────────────────────────", CYAN))
        sys.exit(1)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key)
    KEY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)  # chmod 600
    print(color("│  ✓ ", GREEN) + f"Ключ сохранён в {KEY_FILE}")
    print(color("└──────────────────────────────────────────────────────────", CYAN))
    print()
    return key

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

# ── Сессии ────────────────────────────────────────────────────────────────────

def _cwd_hash() -> str:
    """Короткий хеш текущего каталога — имя подпапки для сессий."""
    return hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:8]


def _sessions_path() -> Path:
    """Путь к каталогу сессий текущего cwd."""
    return SESSIONS_DIR / _cwd_hash()


def _ensure_meta():
    """Создаёт/обновляет _meta.json с полным путём cwd."""
    d = _sessions_path()
    d.mkdir(parents=True, exist_ok=True)
    meta = d / "_meta.json"
    meta.write_text(json.dumps({"path": str(Path.cwd())}, ensure_ascii=False))


def _serialize_content(content):
    """Сериализует content блоки (включая SDK-объекты) в JSON-совместимый формат."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [_serialize_content(item) for item in content]
    if isinstance(content, dict):
        return content
    if hasattr(content, 'model_dump'):
        return content.model_dump()
    return str(content)


def _extract_preview(messages: list, max_len: int = 40) -> str:
    """Первый user-вопрос как краткое описание сессии."""
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            text = m["content"].replace("\n", " ").strip()
            return text[:max_len] + ("…" if len(text) > max_len else "")
    return "—"


def new_session_id() -> str:
    """Генерирует ID новой сессии (ISO-timestamp)."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def save_session(session_id: str, messages: list):
    """Сохраняет сессию на диск в каталог текущего cwd."""
    _ensure_meta()
    # Определяем started из имени файла или первого сообщения
    data = {
        "started": session_id,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "preview": _extract_preview(messages),
        "messages": [
            {"role": m["role"], "content": _serialize_content(m["content"])}
            for m in messages
        ],
    }
    path = _sessions_path() / f"{session_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_session(session_id: str) -> list:
    """Загружает сессию из файла."""
    path = _sessions_path() / f"{session_id}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("messages", [])
    except (json.JSONDecodeError, KeyError):
        return []


def list_sessions(cwd_only: bool = True) -> list:
    """Возвращает информацию о сохранённых сессиях.
    cwd_only=True — только для текущего каталога.
    cwd_only=False — все каталоги.
    """
    if not SESSIONS_DIR.exists():
        return []

    sessions = []
    dirs = [_sessions_path()] if cwd_only else sorted(SESSIONS_DIR.iterdir())

    for d in dirs:
        if not d.is_dir():
            continue
        # Читаем путь из _meta.json
        meta_path = d / "_meta.json"
        cwd_path = "?"
        if meta_path.exists():
            try:
                cwd_path = json.loads(meta_path.read_text()).get("path", "?")
            except (json.JSONDecodeError, KeyError):
                pass

        for f in sorted(d.glob("*.json"), reverse=True):
            if f.name == "_meta.json":
                continue
            try:
                data = json.loads(f.read_text())
                msgs = data.get("messages", [])
                user_msgs = sum(
                    1 for m in msgs
                    if m.get("role") == "user" and isinstance(m.get("content"), str)
                )
                asst_msgs = sum(
                    1 for m in msgs
                    if m.get("role") == "assistant" and isinstance(m.get("content"), str)
                )
                sessions.append({
                    "id": f.stem,
                    "updated": data.get("updated", "?"),
                    "preview": data.get("preview", "—"),
                    "cwd": cwd_path,
                    "user_msgs": user_msgs,
                    "asst_msgs": asst_msgs,
                })
            except (json.JSONDecodeError, KeyError):
                continue

    # Сортировка по updated desc
    sessions.sort(key=lambda s: s["updated"], reverse=True)
    return sessions


def delete_session(session_id: str) -> bool:
    """Удаляет файл сессии из текущего cwd. Возвращает True если существовал."""
    path = _sessions_path() / f"{session_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ── Интерактивное меню ────────────────────────────────────────────────────────

def _top_entries(path: Path = None, n: int = 10) -> list:
    """Top-N видимых объектов из каталога: каталоги первыми, файлы по mtime desc.
    Пропускает hidden (.) и бинарные файлы.
    Возвращает [{"name": str, "is_dir": bool, "size": int}].
    """
    if path is None:
        path = Path.cwd()
    try:
        entries = list(path.iterdir())
    except PermissionError:
        return []

    result = []
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            st = e.stat()
        except (PermissionError, OSError):
            continue
        is_dir = e.is_dir()
        if e.is_file():
            # Быстрая проверка на бинарный файл
            try:
                with e.open("rb") as f:
                    chunk = f.read(512)
                if b"\x00" in chunk:
                    continue
            except (PermissionError, OSError):
                continue
        result.append({
            "name": e.name + ("/" if is_dir else ""),
            "is_dir": is_dir,
            "size": st.st_size if not is_dir else 0,
            "mtime": st.st_mtime,
        })

    # Каталоги первыми, затем файлы по mtime desc
    result.sort(key=lambda x: (not x["is_dir"], -x["mtime"]))
    return result[:n]


def _format_session_label(s: dict) -> str:
    """Форматирует строку для questionary select."""
    try:
        dt = datetime.fromisoformat(s["updated"])
        ts = dt.strftime("%d.%m %H:%M")
    except (ValueError, KeyError):
        ts = s.get("updated", "?")
    return f"{ts}  ({s['user_msgs']}↑ {s['asst_msgs']}↓)  {s['preview']}"


def _select_session(sessions: list) -> str:
    """Меню выбора сессии с 'd' для удаления. Возвращает session_id или '__new__'."""
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings

    while True:
        choices = []
        for s in sessions[:5]:
            choices.append(questionary.Choice(
                title=_format_session_label(s),
                value=s["id"],
            ))
        if len(sessions) > 5:
            choices.append(questionary.Separator(f"  … и ещё {len(sessions) - 5} сессий"))
        choices.append(questionary.Separator())
        choices.append(questionary.Choice(title="✚ Новая сессия", value="__new__"))

        if not sessions:
            return "__new__"

        q = questionary.select("Сессия (d — удалить):", choices=choices)

        # Keybinding 'd' → удалить выбранную сессию
        kb = KeyBindings()

        @kb.add("d")
        def _handle_d(event):
            for w in event.app.layout.find_all_windows():
                ctrl = w.content
                if hasattr(ctrl, "pointed_at") and hasattr(ctrl, "choices"):
                    choice = ctrl.choices[ctrl.pointed_at]
                    val = getattr(choice, "value", None)
                    if val and val != "__new__":
                        event.app.exit(result="__del__" + str(val))
                    return

        app = q.application
        app.key_bindings = merge_key_bindings([app.key_bindings, kb])
        result = q.ask()

        if result is None:  # Ctrl+C
            sys.exit(0)
        if isinstance(result, str) and result.startswith("__del__"):
            sid = result[7:]
            if delete_session(sid):
                sessions = [s for s in sessions if s["id"] != sid]
                print(color(f"  ✓ Сессия удалена", GREEN))
            continue
        return result


def interactive_menu() -> tuple:
    """Интерактивное меню при запуске `ai` без аргументов.
    Возвращает (messages, extra_dirs, extra_files, session_id).
    """
    sessions = list_sessions(cwd_only=True)
    cwd = Path.cwd()

    # ── Шаг 1: выбор сессии ──────────────────────────────────────────
    session_id = _select_session(sessions)

    # Загружаем историю или начинаем новую
    if session_id != "__new__":
        messages = load_session(session_id)
    else:
        session_id = new_session_id()
        messages = []

    # ── Шаг 2: контекст ──────────────────────────────────────────────
    entries = _top_entries(cwd)
    ctx_choices = [
        questionary.Choice(
            title=f"📁 передать листинг каталога ({cwd.name}/)",
            value="__dir__",
            checked=False,
        ),
    ]
    for e in entries:
        ctx_choices.append(questionary.Choice(
            title=e["name"],
            value=e["name"].rstrip("/"),
            checked=False,
        ))

    if ctx_choices:
        selected = questionary.checkbox(
            "Контекст (Space — выбрать, Enter — продолжить):",
            choices=ctx_choices,
        ).ask()
        if selected is None:  # Ctrl+C
            sys.exit(0)
    else:
        selected = []

    extra_dirs = []
    extra_files = []
    for item in selected:
        if item == "__dir__":
            extra_dirs.append(str(cwd))
        else:
            p = cwd / item
            if p.is_dir():
                extra_dirs.append(str(p))
            elif p.is_file():
                extra_files.append(str(p))

    return messages, extra_dirs, extra_files, session_id


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
              ai -s deploy "что в логах?"     # именованная сессия
              ai -s deploy "а что было вчера?" # продолжение
              ai --no-save "быстрый вопрос"   # без сохранения сессии
              ai --sessions                    # список сессий для каталога
              ai --sessions --all              # список всех сессий
              ai           # интерактивный режим с меню
        """),
    )
    parser.add_argument("query", nargs="?", help="Вопрос (если не указан — интерактивный режим)")
    parser.add_argument("-d", "--dir",  action="append", default=[], metavar="PATH", help="Дополнительный каталог для контекста")
    parser.add_argument("-f", "--file", action="append", default=[], metavar="PATH", help="Дополнительный файл для контекста")
    parser.add_argument("-s", "--session", metavar="NAME", help="Имя сессии (обходит интерактивное меню)")
    parser.add_argument("--no-save", action="store_true", help="Не сохранять сессию (одноразовый вопрос)")
    parser.add_argument("--sessions", action="store_true", help="Показать список сессий для текущего каталога")
    parser.add_argument("--all", action="store_true", help="Вместе с --sessions: показать сессии всех каталогов")
    parser.add_argument("--clear-session", metavar="ID", help="Удалить сессию по ID")
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

    # ── Список сессий ────────────────────────────────────────────────
    if args.sessions:
        sessions = list_sessions(cwd_only=not args.all)
        if not sessions:
            label = "Нет сохранённых сессий." if args.all else f"Нет сессий для {Path.cwd()}."
            print(color(label, DIM))
            return
        if not args.all:
            print(color(f"Сессии для {Path.cwd()}:", BOLD))
        else:
            print(color("Все сессии:", BOLD))
        for s in sessions:
            label = _format_session_label(s)
            cwd_info = f"  {DIM}{s['cwd']}{RESET}" if args.all else ""
            print(f"  {color(s['id'], CYAN + BOLD)}  {label}{cwd_info}")
        return

    # ── Удаление сессии ──────────────────────────────────────────────
    if args.clear_session:
        if delete_session(args.clear_session):
            print(color(f"✓ Сессия «{args.clear_session}» удалена.", GREEN))
        else:
            print(color(f"✗ Сессия «{args.clear_session}» не найдена.", BOLD), file=sys.stderr)
            sys.exit(1)
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # ── Валидация путей ──────────────────────────────────────────────
    for d in args.dir:
        if not Path(d).expanduser().is_dir():
            print(color(f"✗ Каталог не найден: {d}", BOLD), file=sys.stderr)
            print(color(f"  Подсказка: -d — флаг для каталога, а не вопрос.", DIM), file=sys.stderr)
            print(color(f"  Используйте:  ai \"ваш вопрос\"", DIM), file=sys.stderr)
            sys.exit(1)
    for f in args.file:
        if not Path(f).expanduser().is_file():
            print(color(f"✗ Файл не найден: {f}", BOLD), file=sys.stderr)
            sys.exit(1)

    api_key = read_api_key()
    client  = anthropic.Anthropic(api_key=api_key)
    system  = read_system_prompt()

    # ── Режим одного вопроса ──────────────────────────────────────────────
    if args.query:
        # Определяем сессию
        if args.session:
            session_id = args.session
            messages = load_session(session_id)
        elif args.no_save:
            session_id = None
            messages = []
        else:
            session_id = new_session_id()
            messages = []

        ctx = build_context(args.dir, args.file)
        full_system = system + "\n\n" + ctx

        messages.append({"role": "user", "content": args.query})
        answer = ask(client, messages, full_system)
        messages.append({"role": "assistant", "content": answer})

        if session_id is not None:
            save_session(session_id, messages)

        render(answer)
        return

    # ── Интерактивный режим ────────────────────────────────────────────────
    print(color("Claude AI · терминал-ассистент", BOLD + CYAN))
    print(color(f"  Каталог: {Path.cwd()}  |  Модель: {MODEL}", DIM))

    if args.session:
        # Прямой режим с именованной сессией (обходим меню)
        session_id = args.session
        messages = load_session(session_id)
        extra_dirs = list(args.dir)
        extra_files = list(args.file)
        if messages:
            n = sum(1 for m in messages if m["role"] == "user")
            print(color(f"  ↻ Сессия «{session_id}» ({n} обменов)", DIM + YELLOW))
    elif _tty():
        # Интерактивное меню
        print()
        messages, menu_dirs, menu_files, session_id = interactive_menu()
        extra_dirs = list(args.dir) + menu_dirs
        extra_files = list(args.file) + menu_files
        if load_session(session_id) != []:
            n = sum(1 for m in messages if m["role"] == "user")
            print(color(f"\n  ↻ Загружена сессия ({n} обменов)", DIM + YELLOW))
    else:
        # Не-TTY: новая сессия без меню
        session_id = new_session_id()
        messages = []
        extra_dirs = list(args.dir)
        extra_files = list(args.file)

    ctx = build_context(extra_dirs, extra_files)
    full_system = system + "\n\n" + ctx

    print(color("  Введите вопрос или 'exit' для выхода\n", DIM))

    while True:
        try:
            if _tty():
                print(color("❯ ", BOLD + CYAN), end="", flush=True)
            query = sys.stdin.buffer.readline().decode("utf-8", errors="replace").strip()
        except KeyboardInterrupt:
            print()
            break
        if not query:
            break
        if query.lower() in ("exit", "quit", "q", "выход"):
            break

        messages.append({"role": "user", "content": query})
        try:
            answer = ask(client, messages, full_system)
        except KeyboardInterrupt:
            messages.pop()  # убираем незавершённый вопрос
            print(color("\n  ⏹ Прервано", DIM))
            continue
        messages.append({"role": "assistant", "content": answer})

        # Автосохранение после каждого обмена
        if not args.no_save:
            save_session(session_id, messages)

        print()
        render(answer)
        print()


if __name__ == "__main__":
    main()

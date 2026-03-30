#!/usr/bin/env bash
# ai — Claude terminal assistant
# Usage: curl -fsSL https://raw.githubusercontent.com/YOUR_USER/ai/main/install.sh | bash
set -euo pipefail

# ── Цвета ────────────────────────────────────────────────────────────────────
RESET='\033[0m'
BOLD='\033[1m'
GREEN='\033[32m'
CYAN='\033[36m'
YELLOW='\033[33m'
RED='\033[31m'
DIM='\033[2m'

info()    { echo -e "  ${CYAN}•${RESET} $*"; }
success() { echo -e "  ${GREEN}✓${RESET} $*"; }
warn()    { echo -e "  ${YELLOW}!${RESET} $*"; }
error()   { echo -e "  ${RED}✗${RESET} $*" >&2; exit 1; }
bold()    { echo -e "${BOLD}$*${RESET}"; }
dim()     { echo -e "${DIM}$*${RESET}"; }

# ── Параметры ─────────────────────────────────────────────────────────────────
GITHUB_USER="mutiev"      # ← замените на ваш GitHub username
GITHUB_REPO="ai-command-helper"             # ← репозиторий
BRANCH="main"

RAW_URL="https://raw.githubusercontent.com/${GITHUB_USER}/${GITHUB_REPO}/${BRANCH}/ai.py"

INSTALL_DIR="${AI_INSTALL_DIR:-$HOME/.local/bin}"
CONFIG_DIR="$HOME/.config/ai"
KEY_FILE="$CONFIG_DIR/api_key"

# ── Баннер ────────────────────────────────────────────────────────────────────
echo ""
bold "  ╔═══════════════════════════════╗"
bold "  ║   ai — Claude в терминале    ║"
bold "  ╚═══════════════════════════════╝"
echo ""
dim "  Репозиторий: https://github.com/${GITHUB_USER}/${GITHUB_REPO}"
echo ""

# ── Проверки ─────────────────────────────────────────────────────────────────

# Python 3.8+
if ! command -v python3 &>/dev/null; then
    error "Python 3 не найден. Установите его (apt install python3 / yum install python3)"
fi

PY_VER=$(python3 -c 'import sys; print(sys.version_info[:2] >= (3, 8))')
if [[ "$PY_VER" != "True" ]]; then
    error "Требуется Python 3.8+. Текущая версия: $(python3 --version)"
fi
success "Python $(python3 --version | cut -d' ' -f2)"

# pip
if ! python3 -m pip --version &>/dev/null 2>&1; then
    warn "pip не найден — пробую установить через ensurepip..."
    python3 -m ensurepip --upgrade 2>/dev/null || \
        error "pip не найден. Установите: apt install python3-pip"
fi

# curl или wget
if command -v curl &>/dev/null; then
    DOWNLOADER="curl"
elif command -v wget &>/dev/null; then
    DOWNLOADER="wget"
else
    error "Нужен curl или wget"
fi
success "Загрузчик: $DOWNLOADER"

# ── Создаём каталоги ──────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"
mkdir -p "$CONFIG_DIR"

# ── Загружаем скрипт ─────────────────────────────────────────────────────────
info "Загружаю ai из GitHub..."

TMPFILE=$(mktemp /tmp/ai_install.XXXXXX)
trap 'rm -f "$TMPFILE"' EXIT

if [[ "$DOWNLOADER" == "curl" ]]; then
    curl -fsSL "$RAW_URL" -o "$TMPFILE" \
        || error "Не удалось загрузить: $RAW_URL"
else
    wget -qO "$TMPFILE" "$RAW_URL" \
        || error "Не удалось загрузить: $RAW_URL"
fi

# Проверяем что скачали питоновский скрипт, а не страницу ошибки
if ! head -1 "$TMPFILE" | grep -q python; then
    error "Файл скачан некорректно (возможно неверный URL или репозиторий приватный)"
fi

cp "$TMPFILE" "$INSTALL_DIR/ai"
chmod +x "$INSTALL_DIR/ai"
success "Скрипт установлен: $INSTALL_DIR/ai"

# ── Устанавливаем зависимость ─────────────────────────────────────────────────
VENV_DIR="$HOME/.local/share/ai/venv"

info "Проверяю Python-зависимость (anthropic)..."

# Проверяем: anthropic уже доступен в системе или в venv?
if python3 -c "import anthropic" 2>/dev/null; then
    success "anthropic уже установлен (системный)"
elif "$VENV_DIR/bin/python" -c "import anthropic" 2>/dev/null; then
    success "anthropic уже установлен (venv: $VENV_DIR)"
elif python3 -m pip install -q --user anthropic 2>/dev/null; then
    success "anthropic установлен"
else
    warn "Обнаружено externally-managed окружение (PEP 668) — создаю venv..."
    mkdir -p "$(dirname "$VENV_DIR")"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install -q anthropic
    success "anthropic установлен в venv: $VENV_DIR"
fi

# ── PATH ─────────────────────────────────────────────────────────────────────
# Проверяем, есть ли INSTALL_DIR в PATH
add_to_path() {
    local shell_file="$1"
    local line='export PATH="$HOME/.local/bin:$PATH"'
    if [[ -f "$shell_file" ]] && ! grep -q '.local/bin' "$shell_file" 2>/dev/null; then
        echo "" >> "$shell_file"
        echo "# ai: Claude terminal assistant" >> "$shell_file"
        echo "$line" >> "$shell_file"
        success "Добавлен PATH в $shell_file"
    fi
}

if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
    warn "$INSTALL_DIR не в PATH — добавляю в shell-конфиги"
    add_to_path "$HOME/.bashrc"
    add_to_path "$HOME/.bash_profile"
    add_to_path "$HOME/.zshrc"
    add_to_path "$HOME/.profile"
    export PATH="$INSTALL_DIR:$PATH"
fi

# ── API-ключ ─────────────────────────────────────────────────────────────────
echo ""
bold "  API-ключ Anthropic"
echo ""

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "$ANTHROPIC_API_KEY" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    success "Ключ сохранён из переменной окружения ANTHROPIC_API_KEY"
elif [[ -f "$KEY_FILE" ]] && [[ -s "$KEY_FILE" ]]; then
    success "Ключ уже есть в $KEY_FILE — пропускаю"
else
    echo -e "  Получить ключ: ${CYAN}https://console.anthropic.com/settings/keys${RESET}"
    echo ""
    if [[ -t 0 ]]; then
        # Интерактивный режим
        read -rp "  Введите API-ключ (или Enter чтобы пропустить): " api_key
        api_key="${api_key//[[:space:]]/}"
        if [[ -n "$api_key" ]]; then
            echo "$api_key" > "$KEY_FILE"
            chmod 600 "$KEY_FILE"
            success "Ключ сохранён в $KEY_FILE"
        else
            warn "Ключ не указан. Задайте позже:"
            dim "    echo 'sk-ant-...' > $KEY_FILE && chmod 600 $KEY_FILE"
            dim "    # или: export ANTHROPIC_API_KEY=sk-ant-..."
        fi
    else
        # Pipe-режим (curl | bash)
        warn "Ключ не указан — ${BOLD}ai${RESET} запросит его при первом запуске и сохранит автоматически."
    fi
fi

# ── Готово ────────────────────────────────────────────────────────────────────
echo ""
bold "  ════════════════════════════════"
success "Установка завершена!"
bold "  ════════════════════════════════"
echo ""
echo -e "  Перезапустите сессию или выполните:"
echo -e "  ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}"
echo ""
echo -e "  Попробуйте:"
echo -e "  ${BOLD}ai \"сколько места на диске?\"${RESET}"
echo -e "  ${BOLD}ai -d /etc/nginx \"объясни конфиг\"${RESET}"
echo -e "  ${BOLD}ai${RESET}  ${DIM}# интерактивный режим${RESET}"
echo ""
echo -e "  ${DIM}Обновление: повторно запустите install.sh${RESET}"
echo ""

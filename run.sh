#!/usr/bin/env sh
# Однокликовый запуск локального веб-интерфейса «Захоронения Москвы» (Linux / macOS).
# Не требует сторонних пакетов (работает на стандартной библиотеке Python 3.8+).

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Ошибка: не найден интерпретатор Python 3 (python3)." >&2
    echo "Установите пакет python3 через пакетный менеджер вашего дистрибутива." >&2
    exit 1
fi

exec "$PYTHON_BIN" -m scripts.web "$@"

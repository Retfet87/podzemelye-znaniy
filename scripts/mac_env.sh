#!/bin/sh
# Shared launcher setup; never source .env as shell code.
set -eu
MAC_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$MAC_ROOT"
if [ -z "${BOT_PYTHON:-}" ] && [ -f .runtime/mac-python ]; then
    BOT_PYTHON=$(cat .runtime/mac-python)
fi
if [ -z "${BOT_PYTHON:-}" ]; then
    for candidate in "$MAC_ROOT/.venv/bin/python" python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info[:2] == (3,12)' 2>/dev/null; then
            BOT_PYTHON=$candidate
            break
        fi
    done
fi
if [ -z "${BOT_PYTHON:-}" ] || ! "$BOT_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3,12)' 2>/dev/null; then
    echo 'Нужен Python 3.12. Укажи путь к окружению в BOT_PYTHON; см. START_HERE.md.' >&2
    exit 1
fi
if [ -d .runtime/pg-client/bin ]; then PATH="$MAC_ROOT/.runtime/pg-client/bin:$PATH"; fi
if [ -d .runtime/media-bin ]; then PATH="$MAC_ROOT/.runtime/media-bin:$PATH"; fi
export PATH
if ! "$BOT_PYTHON" -c 'import psycopg, psycopg_pool, xlrd, PIL' 2>/dev/null; then
    echo 'Не хватает зависимостей. Установи requirements.txt в выбранное окружение; см. START_HERE.md.' >&2
    exit 1
fi

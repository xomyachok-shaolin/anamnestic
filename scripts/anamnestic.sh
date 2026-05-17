#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

export PYTHONPATH="$REPO_ROOT"
export ANAMNESTIC_DATA_DIR="${ANAMNESTIC_DATA_DIR:-${CLAUDE_MEM_DATA_DIR:-$HOME/.claude-mem}}"
export ANAMNESTIC_PROJECT_PREFIXES="${ANAMNESTIC_PROJECT_PREFIXES:-}"
export ANAMNESTIC_CC_ROOT="${ANAMNESTIC_CC_ROOT:-$HOME/.claude/projects}"
export ANAMNESTIC_CODEX_ROOT="${ANAMNESTIC_CODEX_ROOT:-$HOME/.codex/sessions}"

PYTHON_BIN="${ANAMNESTIC_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in \
        "$REPO_ROOT/.venv/bin/python" \
        "$HOME/.claude-mem/semantic-env/bin/python" \
        python3 \
        python
    do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi

if [[ -z "$PYTHON_BIN" ]]; then
    echo "anamnestic: no Python interpreter found; set ANAMNESTIC_PYTHON" >&2
    exit 127
fi

exec "$PYTHON_BIN" -m anamnestic.cli "$@"

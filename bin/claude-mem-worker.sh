#!/usr/bin/env bash
# Foreground wrapper for the bundled claude-mem worker-service.cjs.
set -euo pipefail

shopt -s nullglob
node_bins=("$HOME"/.nvm/versions/node/v*/bin)
if ((${#node_bins[@]} > 0)); then
    newest_node_bin=$(printf '%s\n' "${node_bins[@]}" | sort -V | tail -n1)
else
    newest_node_bin=""
fi

export PATH="$HOME/.bun/bin${newest_node_bin:+:$newest_node_bin}:$HOME/.local/share/pnpm:/usr/local/bin:/usr/bin:/bin"

if ! command -v bun >/dev/null 2>&1; then
    echo "bun not found in PATH=$PATH" >&2
    exit 127
fi

resolve_worker_script() {
    if [[ -n "${CLAUDE_MEM_WORKER_SCRIPT:-}" ]]; then
        [[ -f "$CLAUDE_MEM_WORKER_SCRIPT" ]] && printf '%s\n' "$CLAUDE_MEM_WORKER_SCRIPT" && return 0
        echo "CLAUDE_MEM_WORKER_SCRIPT does not exist: $CLAUDE_MEM_WORKER_SCRIPT" >&2
        return 1
    fi

    if [[ -n "${CLAUDE_MEM_PLUGIN_DIR:-}" ]]; then
        local explicit_script="$CLAUDE_MEM_PLUGIN_DIR/scripts/worker-service.cjs"
        [[ -f "$explicit_script" ]] && printf '%s\n' "$explicit_script" && return 0
        echo "CLAUDE_MEM_PLUGIN_DIR does not contain scripts/worker-service.cjs: $CLAUDE_MEM_PLUGIN_DIR" >&2
        return 1
    fi

    local roots=(
        "$HOME/.claude/plugins/marketplaces"
        "$HOME/.claude/plugins/cache"
    )
    local root script plugin_dir package_json

    for root in "${roots[@]}"; do
        [[ -d "$root" ]] || continue
        while IFS= read -r script; do
            plugin_dir=$(cd -P "$(dirname "$script")/.." && pwd)
            package_json="$plugin_dir/package.json"
            if [[ -f "$package_json" ]] && grep -q '"name"[[:space:]]*:[[:space:]]*"claude-mem-plugin"' "$package_json"; then
                printf '%s\n' "$script"
                return 0
            fi
        done < <(find -L "$root" -path '*/scripts/worker-service.cjs' -type f 2>/dev/null | sort -Vr)
    done

    return 1
}

if ! worker_script=$(resolve_worker_script); then
    echo "claude-mem plugin with scripts/worker-service.cjs not found" >&2
    exit 1
fi
worker_script=$(cd -P "$(dirname "$worker_script")" && pwd)/$(basename "$worker_script")

mkdir -p "$HOME/.claude-mem/logs"
plugin_dir=$(cd -P "$(dirname "$worker_script")/.." && pwd)
cd "$plugin_dir"

cmd="${1:-foreground}"
exec bun "$worker_script" "$cmd"

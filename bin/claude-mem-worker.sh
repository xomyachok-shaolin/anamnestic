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

plugin_candidates=(
    "${CLAUDE_MEM_PLUGIN_DIR:-}"
    "$HOME/.claude/plugins/marketplaces/thedotmack/plugin"
    "$HOME/.claude/plugins/cache/thedotmack/claude-mem/13.2.0"
    "$HOME/.claude/plugins/cache/thedotmack/claude-mem/12.1.2"
    "$HOME/.claude/plugins/cache/thedotmack/claude-mem/12.1.0"
)

plugin_dir=""
for candidate in "${plugin_candidates[@]}"; do
    if [[ -n "$candidate" && -f "$candidate/scripts/worker-service.cjs" ]]; then
        plugin_dir=$(cd -P "$candidate" && pwd)
        break
    fi
done

if [[ -z "$plugin_dir" ]]; then
    echo "claude-mem plugin with scripts/worker-service.cjs not found" >&2
    exit 1
fi

mkdir -p "$HOME/.claude-mem/logs"
cd "$plugin_dir"

cmd="${1:-foreground}"
exec bun scripts/worker-service.cjs "$cmd"

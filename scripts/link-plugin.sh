#!/usr/bin/env bash
#
# Activate the repo's plugins for every project on this machine (run once per clone).
#
# Symlinks each plugin under `plugins/` into `~/.claude/skills/`, where Claude
# Code auto-discovers any directory containing `.claude-plugin/plugin.json` as
# a `<name>@skills-dir` plugin — personal scope, loaded in every project, no
# marketplace and no install step.
#
# The canonical copy stays in version control here, so a fix propagates to
# every project at once instead of being copy-pasted per repo and drifting.
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

PLUGIN_SRC="$REPO_ROOT/plugins"
SKILLS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"

if [ ! -d "$PLUGIN_SRC" ]; then
    echo "link-plugin: plugins/ not found at repo root" >&2
    exit 1
fi

mkdir -p "$SKILLS_DIR"

linked=0
for plugin in "$PLUGIN_SRC"/*/; do
    [ -d "$plugin" ] || continue
    name="$(basename "$plugin")"
    manifest="$plugin/.claude-plugin/plugin.json"

    if [ ! -f "$manifest" ]; then
        echo "  skip  $name (no .claude-plugin/plugin.json)"
        continue
    fi

    target="$SKILLS_DIR/$name"
    plugin_abs="${plugin%/}"

    if [ -L "$target" ]; then
        current="$(readlink "$target")"
        if [ "$current" = "$plugin_abs" ]; then
            echo "  ok    $name (already linked)"
            linked=$((linked + 1))
            continue
        fi
        echo "  relink $name (was -> $current)"
        rm "$target"
    elif [ -e "$target" ]; then
        # A real directory here is someone's own skill or an earlier copy.
        # Refuse rather than delete it.
        echo "link-plugin: $target exists and is not a symlink — not touching it." >&2
        echo "             Move or remove it, then re-run." >&2
        exit 1
    fi

    ln -s "$plugin_abs" "$target"
    chmod +x "$plugin_abs"/hooks/*.py "$plugin_abs"/hooks/*.sh 2>/dev/null || true
    echo "  link  $name -> $target"
    linked=$((linked + 1))
done

echo
echo "✓ $linked plugin(s) linked into $SKILLS_DIR"
echo "  Start a new session, then confirm with:  /plugin"
echo "  After editing a hook:                    /reload-plugins"
echo "  Uninstall:                               rm $SKILLS_DIR/<name>"

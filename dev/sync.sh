#!/bin/bash

# Development sync: copy this checkout into the Omarchy plugins folder
# (the shell rejects symlinked plugins) and enable it in the bar.
#
#   dev/sync.sh            copy + enable
#   dev/sync.sh --copy     copy only (QML hot-reloads; restart the bridge with
#                          `pkill -f '[j]bl-bridge'` after Python changes)

set -euo pipefail

cd "$(dirname "$0")/.."
ID=$(jq -r .id manifest.json)
TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$ID"

omarchy plugin validate .

mkdir -p "$TARGET"
rsync -a --delete \
  --exclude .git --exclude .claude --exclude tests --exclude dev --exclude tools \
  --exclude __pycache__ --exclude .gitignore \
  ./ "$TARGET/"
echo "Copied to $TARGET"

if [[ ${1:-} != --copy ]]; then
  omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
  omarchy plugin enable "$ID"
fi

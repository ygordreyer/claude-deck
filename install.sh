#!/bin/bash
# Copy the built plugin into Decky's (root-owned) plugin folder and reload Decky.
# Usage: sudo ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
DEST=/home/deck/homebrew/plugins/claude-deck
[[ -f dist/index.js ]] || { echo "dist/index.js missing - run: distrobox enter dev -- npm run build"; exit 1; }
rm -rf "$DEST"
mkdir -p "$DEST/dist"
cp plugin.json package.json main.py steam_mcp.py deck_tools.py fw_transcribe.py "$DEST/"
cp dist/* "$DEST/dist/"
systemctl restart plugin_loader
echo "Installed to $DEST and restarted Decky."

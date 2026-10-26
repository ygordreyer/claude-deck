#!/bin/bash
# Build on this machine and install on the Deck over SSH (SteamOS has no Node).
# Copies files into the installed plugin, then disables and re-enables it in Decky to reload.
# Refuses while Claude is mid-reply unless --force, since a reload stops the reply.
# Usage: scripts/deploy.sh [--force] [deck-host]   (default $DECK_HOST or deck@steamdeck)
set -euo pipefail
cd "$(dirname "$0")/.."
FORCE=0; [[ "${1:-}" == --force ]] && { FORCE=1; shift; }
DECK=${1:-${DECK_HOST:-deck@steamdeck}}
npm run build --silent
if [[ $FORCE == 0 ]]; then
  busy=$(ssh "$DECK" 'cd ~/claude-deck && python3 -c "
import asyncio, json
from steam_mcp import cdp_eval
s = json.loads(asyncio.run(cdp_eval(\"return await DeckyBackend.call(\\\"loader/call_plugin_method\\\", \\\"Claude\\\", \\\"get_state\\\");\", {})))
print(\"busy\" if s[\"running\"] or s[\"asks\"] else \"idle\")
" 2>/dev/null || echo idle')
  [[ $busy == busy ]] && { echo "Claude is busy on the Deck; try again later or pass --force." >&2; exit 1; }
fi
git push -q deck HEAD:refs/heads/incoming
ssh "$DECK" 'cd ~/claude-deck && git merge -q --ff-only incoming'
rsync -a --delete dist/ "$DECK":claude-deck/dist/
ssh "$DECK" 'cd ~/claude-deck && D=~/homebrew/plugins/claude-deck && cp dist/* $D/dist/ &&
  for f in steam_mcp.py deck_tools.py fw_transcribe.py package.json main.py; do cat $f > $D/$f; done && grep version $D/package.json &&
  python3 reload.py'

#!/bin/zsh
# Replace CLAUDE_CODE_OAUTH_TOKEN in .env with a new token and restart the bridge.
# Usage: run `claude setup-token` first, copy the token it prints, then run this script.
set -e
cd "$(dirname "$0")"
printf 'Paste new Claude OAuth token (input hidden): '
read -rs TOK
echo
if [[ -z "$TOK" || ${#TOK} -lt 40 ]]; then echo "No token / too short. Aborting."; exit 1; fi
cp .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
if grep -qE '^CLAUDE_CODE_OAUTH_TOKEN=' .env; then
  python3 - "$TOK" <<'PY'
import re,sys
tok=sys.argv[1]
p='.env'; s=open(p).read()
s=re.sub(r'^CLAUDE_CODE_OAUTH_TOKEN=.*$', 'CLAUDE_CODE_OAUTH_TOKEN='+tok, s, flags=re.M)
open(p,'w').write(s)
PY
else
  echo "CLAUDE_CODE_OAUTH_TOKEN=$TOK" >> .env
fi
echo "Token written. Verifying headless login..."
OUT=$(echo "Reply with exactly: OK" | CLAUDE_CODE_OAUTH_TOKEN="$TOK" CLAUDE_CONFIG_DIR="$HOME/.claude-lark" timeout 90 claude -p --output-format text 2>&1 | tail -1)
echo "claude says: $OUT"
if [[ "$OUT" != *OK* ]]; then echo "Verification failed. .env backup kept; bridge NOT restarted."; exit 1; fi
# LaunchAgent to restart: LARK_CLAUDE_PLIST if set, else the first com.*.lark-claude.plist found.
plists=(~/Library/LaunchAgents/com.*.lark-claude.plist(N))
PLIST="${LARK_CLAUDE_PLIST:-${plists[1]}}"
if [[ -z "$PLIST" ]]; then echo "Token saved, but no lark-claude LaunchAgent was found. Restart the bridge yourself."; exit 1; fi
launchctl unload "$PLIST" 2>/dev/null || true
sleep 2
launchctl load "$PLIST"
sleep 3
pgrep -fl "lark-claudecode-bridge/main.py" >/dev/null && echo "Bridge restarted OK." || echo "Bridge did not come back; check stdout.log"

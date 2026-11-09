#!/bin/zsh
# Journal Scanner launcher (runs inside "Journal Scanner.app").
# 1. If the scanner is already running: offer Open or Stop.
# 2. Otherwise: open Ollama, mount the vault (macOS asks for its password),
#    start the scanner server in the background, open it in the browser.
# Everything stays on this Mac. The server log has file names, never page text.

INTAKE="${JS_HOME:-__SCANNER_DIR__}"   # filled in by build_app.sh
PORT=8770
URL="http://127.0.0.1:$PORT"
LOG="$HOME/Library/Logs/JournalScanner.log"
VAULT_NAME="${JS_VAULT_NAME:-JournalVault}"   # the encrypted disk image's name (see CUSTOMIZE.md)
VAULT="${JS_VAULT:-/Volumes/$VAULT_NAME}"
PY=/usr/bin/python3

say_dialog() {  # $1 message, $2.. buttons (last is default)
  local msg="$1"; shift
  local btns=$(printf '"%s", ' "$@"); btns=${btns%, }
  osascript -e "button returned of (display dialog \"$msg\" with title \"Journal Scanner\" buttons {$btns} default button \"${@[-1]}\" with icon note)" 2>/dev/null
}
running() { curl -s -m 1 "$URL/api/status" >/dev/null 2>&1; }

if running; then
  choice=$(say_dialog "Journal Scanner is already running." "Stop it" "Open")
  if [[ "$choice" == "Stop it" ]]; then
    pkill -f "journal_scanner.py --port $PORT" && osascript -e 'display notification "Stopped." with title "Journal Scanner"'
  elif [[ -n "$choice" ]]; then
    open "$URL"
  fi
  exit 0
fi

if [[ ! -f "$INTAKE/journal_scanner.py" ]]; then
  say_dialog "Can't find $INTAKE/journal_scanner.py." "OK"; exit 1
fi

# Ollama (local models). Hidden, in the background.
pgrep -qf "ollama serve" || open -ga Ollama 2>/dev/null

# The vault: open the encrypted image if it isn't mounted yet.
if [[ ! -d "$VAULT" ]]; then
  img=$(ls -d "$HOME"/$VAULT_NAME.{sparsebundle,sparseimage,dmg} "$HOME"/Documents/$VAULT_NAME.{sparsebundle,sparseimage,dmg} \
            /Volumes/*/$VAULT_NAME.{sparsebundle,sparseimage,dmg} /Volumes/*/*/$VAULT_NAME.{sparsebundle,sparseimage,dmg} 2>/dev/null | head -1)
  if [[ -n "$img" ]]; then
    open "$img"   # macOS asks for the vault password
    for i in {1..120}; do [[ -d "$VAULT" ]] && break; sleep 1; done
  fi
  if [[ -d "$VAULT" ]]; then
    mkdir -p "$VAULT/journals"
  else
    choice=$(say_dialog "$VAULT_NAME isn't mounted, so pages can't be saved yet. Open it in Finder (or create it: see the README), and the scanner notices on its own." "Quit" "Start anyway")
    [[ "$choice" == "Start anyway" ]] || exit 0
  fi
else
  mkdir -p "$VAULT/journals"
fi

# Start the server, detached, and wait for it to answer.
mkdir -p "${LOG:h}"
cd "$INTAKE"
nohup "$PY" journal_scanner.py --port $PORT --no-open --out "$VAULT/journals" >>"$LOG" 2>&1 &
for i in {1..40}; do running && break; sleep 0.25; done
if ! running; then
  say_dialog "The scanner didn't start. Details are in $LOG." "OK"; exit 1
fi
open "$URL"

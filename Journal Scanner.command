#!/bin/zsh
# Double-click to start Journal Scanner. Close this window (or Ctrl-C) to stop it.
cd "$(dirname "$0")"
exec python3 journal_scanner.py "$@"

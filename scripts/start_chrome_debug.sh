#!/usr/bin/env bash
set -euo pipefail

CDP_PORT="${JOB_AUTOMATOR_CDP_PORT:-9222}"
DEBUG_DIR="${JOB_AUTOMATOR_CHROME_DEBUG_DIR:-$HOME/ChromeDebug}"
mkdir -p "$DEBUG_DIR"

CHROME_BIN=""
for cand in \
  "google-chrome" \
  "chromium" \
  "chromium-browser" \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
  if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
    CHROME_BIN="$cand"
    break
  fi
done

if [ -z "$CHROME_BIN" ]; then
  echo "No se encontró Google Chrome ni Chromium en el sistema." >&2
  exit 1
fi

echo "Iniciando Chrome con puerto CDP $CDP_PORT en $DEBUG_DIR..."
nohup "$CHROME_BIN" \
  --remote-debugging-port="$CDP_PORT" \
  --user-data-dir="$DEBUG_DIR" \
  --no-first-run \
  --no-default-browser-check >/dev/null 2>&1 &

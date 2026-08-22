#!/usr/bin/env bash
# Double-clickable launcher for macOS Finder.
# Opens in Terminal, starts the dashboard and then opens it in the browser.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f .venv/bin/activate ]]; then
  echo "No virtual environment found. Running setup first..."
  bash ./setup_macos.sh
fi

# shellcheck source=/dev/null
source .venv/bin/activate

# Read the port from .env so the browser opens the right address.
PORT=8787
if [[ -f .env ]]; then
  ENV_PORT="$(grep -E '^APP_PORT=' .env | tail -n 1 | cut -d= -f2 | tr -d '[:space:]' || true)"
  [[ -n "${ENV_PORT:-}" ]] && PORT="$ENV_PORT"
fi

# Open the dashboard once the server is listening.
(
  for _ in $(seq 1 40); do
    if curl -s -o /dev/null "http://127.0.0.1:${PORT}/health" 2>/dev/null; then
      open "http://127.0.0.1:${PORT}"
      break
    fi
    sleep 0.5
  done
) &

echo
echo "Starting Restream Manager on http://127.0.0.1:${PORT}"
echo "Press Ctrl+C to stop. FFmpeg processes are shut down cleanly on exit."
echo

exec python -m app.main

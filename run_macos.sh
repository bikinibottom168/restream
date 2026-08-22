#!/usr/bin/env bash
# ===========================================================================
# Restream Manager - start the application (macOS / Linux)
# ===========================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; RESET=$'\033[0m'

if [[ ! -f .venv/bin/activate ]]; then
  printf "%s[ERROR]%s No virtual environment found. Run ./setup_macos.sh first.\n" "$RED" "$RESET"
  exit 1
fi

# shellcheck source=/dev/null
source .venv/bin/activate

if [[ ! -f .env ]]; then
  printf "%s[WARN]%s No .env file found - using built-in defaults.\n" "$YELLOW" "$RESET"
fi

echo
echo "Starting Restream Manager..."
echo "Press Ctrl+C to stop. FFmpeg processes are shut down cleanly on exit."
echo

exec python -m app.main

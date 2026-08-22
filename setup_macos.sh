#!/usr/bin/env bash
# ===========================================================================
# Restream Manager - first-time setup for macOS (Intel and Apple Silicon)
# Installs everything needed: Python deps, and downloads FFmpeg + MediaMTX
# into bin/ automatically. Falls back to clear instructions if a step fails.
# ===========================================================================
set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BOLD=$'\033[1m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; RED=$'\033[0;31m'; RESET=$'\033[0m'
ok()   { printf "%s[OK]%s   %s\n" "$GREEN" "$RESET" "$1"; }
warn() { printf "%s[WARN]%s %s\n" "$YELLOW" "$RESET" "$1"; }
fail() { printf "%s[ERROR]%s %s\n" "$RED" "$RESET" "$1"; exit 1; }
step() { printf "%s[..]%s   %s\n" "$BOLD" "$RESET" "$1"; }

printf "\n%s==========================================%s\n" "$BOLD" "$RESET"
printf "%s  Restream Manager - macOS setup%s\n" "$BOLD" "$RESET"
printf "%s==========================================%s\n\n" "$BOLD" "$RESET"

# ---- 0. architecture -------------------------------------------------------
UNAME_M="$(uname -m)"
case "$UNAME_M" in
  arm64)  MTXARCH="arm64" ;;
  x86_64) MTXARCH="amd64" ;;
  *)      MTXARCH="amd64" ;;
esac

# ---- 1. Python -------------------------------------------------------------
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PYTHON="$candidate"; break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  if command -v brew >/dev/null 2>&1; then
    step "Installing Python 3.12 with Homebrew"
    brew install python@3.12 || warn "brew install python@3.12 failed"
    for candidate in python3.12 python3; do
      command -v "$candidate" >/dev/null 2>&1 && PYTHON="$candidate" && break
    done
  fi
fi
[[ -z "$PYTHON" ]] && fail "Python 3.11+ not found. Install Homebrew (https://brew.sh) then: brew install python@3.12"
ok "Using $($PYTHON --version) at $(command -v "$PYTHON")"

# ---- 2. virtual environment ------------------------------------------------
if [[ ! -d .venv ]]; then
  step "Creating virtual environment in .venv"
  "$PYTHON" -m venv .venv
else
  ok "Virtual environment already exists"
fi
# shellcheck source=/dev/null
source .venv/bin/activate

# ---- 3. dependencies -------------------------------------------------------
step "Upgrading pip"
python -m pip install --upgrade pip --quiet
step "Installing Python dependencies (this can take a minute)"
python -m pip install -r requirements.txt || fail "Dependency installation failed"
ok "Dependencies installed"

# ---- 4. .env + folders -----------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  ok "Created .env from .env.example"
else
  ok ".env already exists, leaving it untouched"
fi
mkdir -p data logs/ffmpeg logs/debug data/pids bin
ok "Data, log and bin directories ready"

# ---- 5. FFmpeg -------------------------------------------------------------
echo
if command -v ffmpeg >/dev/null 2>&1; then
  ok "FFmpeg found on PATH: $(ffmpeg -version 2>/dev/null | head -n 1)"
elif [[ -x bin/ffmpeg ]]; then
  ok "FFmpeg already present in bin/"
elif command -v brew >/dev/null 2>&1; then
  step "Installing FFmpeg with Homebrew"
  brew install ffmpeg && ok "FFmpeg installed via Homebrew" || warn "brew install ffmpeg failed - install it manually"
else
  step "Downloading a static FFmpeg build (evermeet.cx)"
  if curl -fL "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" -o /tmp/ffmpeg.zip \
     && curl -fL "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip" -o /tmp/ffprobe.zip; then
    unzip -o /tmp/ffmpeg.zip -d bin/ >/dev/null 2>&1 || true
    unzip -o /tmp/ffprobe.zip -d bin/ >/dev/null 2>&1 || true
    chmod +x bin/ffmpeg bin/ffprobe 2>/dev/null || true
    xattr -dr com.apple.quarantine bin/ffmpeg bin/ffprobe 2>/dev/null || true
    if [[ -x bin/ffmpeg ]]; then ok "FFmpeg installed into bin/"; else warn "Could not place FFmpeg in bin/ - install with: brew install ffmpeg"; fi
  else
    warn "FFmpeg download failed. Install Homebrew (https://brew.sh) then: brew install ffmpeg"
  fi
fi

# ---- 6. MediaMTX (anti-drop buffer) ---------------------------------------
echo
if [[ -x bin/mediamtx ]]; then
  ok "MediaMTX already present in bin/"
else
  step "Finding the latest MediaMTX release"
  MTXTAG="$("$PYTHON" - <<'PY' 2>/dev/null || echo v1.11.3
import json, urllib.request
try:
    with urllib.request.urlopen("https://api.github.com/repos/bluenviron/mediamtx/releases/latest", timeout=15) as r:
        print(json.load(r)["tag_name"])
except Exception:
    print("v1.11.3")
PY
)"
  [[ -z "$MTXTAG" ]] && MTXTAG="v1.11.3"
  MTXURL="https://github.com/bluenviron/mediamtx/releases/download/${MTXTAG}/mediamtx_${MTXTAG}_darwin_${MTXARCH}.tar.gz"
  step "Downloading MediaMTX ${MTXTAG} (${MTXARCH})"
  if curl -fL "$MTXURL" -o /tmp/mediamtx.tar.gz; then
    tar -xzf /tmp/mediamtx.tar.gz -C bin/ mediamtx 2>/dev/null || tar -xzf /tmp/mediamtx.tar.gz -C bin/ 2>/dev/null || true
    chmod +x bin/mediamtx 2>/dev/null || true
    xattr -dr com.apple.quarantine bin/mediamtx 2>/dev/null || true
    if [[ -x bin/mediamtx ]]; then
      ok "MediaMTX installed into bin/. Turn the anti-drop buffer on in Settings."
    else
      warn "MediaMTX archive downloaded but the binary was not found - extract it into bin/ manually."
    fi
  else
    warn "MediaMTX download failed. Get it from https://github.com/bluenviron/mediamtx/releases"
    warn "and put the 'mediamtx' binary into bin/. The app runs fine without it (buffer stays off)."
  fi
fi

# ---- 7. self-check ---------------------------------------------------------
echo
step "Verifying the installation"
python -c "import app.main" && ok "Application imports cleanly"

chmod +x run_macos.command run_macos.sh 2>/dev/null || true

printf "\n%s==========================================%s\n" "$BOLD" "$RESET"
printf "%s  Setup complete%s\n" "$BOLD" "$RESET"
printf "%s==========================================%s\n\n" "$BOLD" "$RESET"
echo "  1. Start it with:  ./run_macos.sh   (or double-click run_macos.command)"
echo "  2. Open http://127.0.0.1:8787"
echo "  3. Optional: Settings -> anti-drop buffer to enable MediaMTX"
echo

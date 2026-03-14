#!/usr/bin/env bash
# =============================================================================
# TokenRanger — full dependency installer
# =============================================================================
# Installs all system + Node + Python + Ollama dependencies needed to run
# the TokenRanger context compression plugin.
#
# Usage:
#   ./scripts/install.sh               # interactive, auto-detects GPU
#   ./scripts/install.sh --cpu-only    # force CPU mode (skips qwen3:8b pull)
#   ./scripts/install.sh --skip-ollama # skip Ollama install + model pull
#   ./scripts/install.sh --skip-build  # skip npm install + tsc build
#
# Supported platforms: macOS (Homebrew), Linux (apt/curl)
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $*${NC}"; }
err()  { echo -e "${RED}  ✗ $*${NC}"; }
hdr()  { echo -e "\n${GREEN}▶ $*${NC}"; }

# ── Flags ─────────────────────────────────────────────────────────────────────
CPU_ONLY=false
SKIP_OLLAMA=false
SKIP_BUILD=false

for arg in "$@"; do
  case "$arg" in
    --cpu-only)    CPU_ONLY=true ;;
    --skip-ollama) SKIP_OLLAMA=true ;;
    --skip-build)  SKIP_BUILD=true ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  TokenRanger — Dependency Installer"
echo "  Project: $PROJECT_ROOT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

OS="$(uname -s)"
ARCH="$(uname -m)"

# ── 1. Node.js ────────────────────────────────────────────────────────────────
hdr "Node.js"

if command -v node &>/dev/null; then
  NODE_VER="$(node --version)"
  ok "node $NODE_VER"
else
  warn "node not found — installing..."
  if [[ "$OS" == "Darwin" ]]; then
    if command -v brew &>/dev/null; then
      brew install node
    else
      err "Homebrew not found. Install Node.js from https://nodejs.org"
      exit 1
    fi
  elif [[ "$OS" == "Linux" ]]; then
    if command -v apt-get &>/dev/null; then
      curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
      sudo apt-get install -y nodejs
    else
      err "Unsupported Linux package manager. Install Node.js from https://nodejs.org"
      exit 1
    fi
  fi
  ok "node $(node --version)"
fi

if command -v npm &>/dev/null; then
  ok "npm $(npm --version)"
else
  err "npm not found after Node.js install"
  exit 1
fi

# ── 2. npm install + TypeScript build ─────────────────────────────────────────
if [[ "$SKIP_BUILD" == false ]]; then
  hdr "Node dependencies + TypeScript build"
  cd "$PROJECT_ROOT"

  echo "  Running npm install..."
  npm install
  ok "node_modules installed"

  echo "  Building TypeScript..."
  npm run build
  ok "dist/ built"
else
  warn "Skipping npm install + build (--skip-build)"
fi

# ── 3. Python ─────────────────────────────────────────────────────────────────
hdr "Python 3"

PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
  if command -v "$cmd" &>/dev/null; then
    VER="$($cmd --version 2>&1 | awk '{print $2}')"
    MAJOR="${VER%%.*}"; MINOR="${VER#*.}"; MINOR="${MINOR%%.*}"
    if [[ "$MAJOR" -ge 3 && "$MINOR" -ge 10 ]]; then
      PYTHON="$cmd"
      ok "$cmd $VER"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  warn "Python >= 3.10 not found — installing..."
  if [[ "$OS" == "Darwin" ]]; then
    brew install python@3.12
    PYTHON="python3.12"
  elif [[ "$OS" == "Linux" ]] && command -v apt-get &>/dev/null; then
    sudo apt-get install -y python3 python3-pip python3-venv
    PYTHON="python3"
  else
    err "Cannot auto-install Python. Please install Python >= 3.10 manually."
    exit 1
  fi
  ok "$PYTHON $($PYTHON --version)"
fi

# ── 4. Python venv + pip deps ─────────────────────────────────────────────────
hdr "Python service dependencies"

SERVICE_DIR="$PROJECT_ROOT/service"
VENV_DIR="$SERVICE_DIR/venv"
REQUIREMENTS="$SERVICE_DIR/requirements.txt"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "  Creating venv at $VENV_DIR..."
  "$PYTHON" -m venv "$VENV_DIR"
  ok "venv created"
else
  ok "venv exists"
fi

PIP="$VENV_DIR/bin/pip"
echo "  Installing Python packages from $REQUIREMENTS..."
"$PIP" install --upgrade pip --quiet
"$PIP" install -r "$REQUIREMENTS" --quiet
ok "Python packages installed"

# ── 5. GPU detection ──────────────────────────────────────────────────────────
hdr "Compute detection"

HAS_GPU=false
GPU_MODEL="qwen3:8b"
CPU_MODEL="qwen3:1.7b"

if [[ "$CPU_ONLY" == true ]]; then
  warn "CPU-only mode forced (--cpu-only)"
elif command -v nvidia-smi &>/dev/null; then
  VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
  if [[ "$VRAM_MB" =~ ^[0-9]+$ && "$VRAM_MB" -ge 4096 ]]; then
    HAS_GPU=true
    ok "NVIDIA GPU detected — ${VRAM_MB}MB VRAM → will use $GPU_MODEL (full compression)"
  else
    warn "NVIDIA GPU found but VRAM < 4GB → falling back to CPU mode"
  fi
elif [[ "$OS" == "Darwin" ]]; then
  # Apple Silicon Metal GPU — Ollama uses Metal by default
  if [[ "$ARCH" == "arm64" ]]; then
    # Check available RAM
    RAM_GB="$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))"
    if [[ "$RAM_GB" -ge 16 ]]; then
      HAS_GPU=true
      ok "Apple Silicon detected (${RAM_GB}GB unified memory) → will use $GPU_MODEL via Metal"
    else
      warn "Apple Silicon but only ${RAM_GB}GB RAM → using CPU model ($CPU_MODEL)"
    fi
  fi
else
  warn "No discrete GPU detected → CPU-only mode ($CPU_MODEL, light compression)"
fi

PULL_MODEL="$( [[ "$HAS_GPU" == true ]] && echo "$GPU_MODEL" || echo "$CPU_MODEL" )"

# ── 6. Ollama ─────────────────────────────────────────────────────────────────
if [[ "$SKIP_OLLAMA" == false ]]; then
  hdr "Ollama"

  if command -v ollama &>/dev/null; then
    ok "ollama $(ollama --version 2>/dev/null || echo 'installed')"
  else
    echo "  Installing Ollama..."
    if [[ "$OS" == "Darwin" ]] && command -v brew &>/dev/null; then
      brew install ollama
    elif [[ "$OS" == "Linux" ]]; then
      curl -fsSL https://ollama.com/install.sh | sh
    else
      err "Cannot auto-install Ollama. Download from https://ollama.com/download"
      exit 1
    fi
    ok "ollama installed"
  fi

  # Ensure Ollama is running
  if ! ollama list &>/dev/null 2>&1; then
    echo "  Starting Ollama..."
    ollama serve &>/dev/null &
    sleep 3
  fi

  # Pull model
  hdr "Ollama model: $PULL_MODEL"
  INSTALLED="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
  if echo "$INSTALLED" | grep -qx "$PULL_MODEL"; then
    ok "$PULL_MODEL already present"
  else
    echo "  Pulling $PULL_MODEL (this may take several minutes)..."
    ollama pull "$PULL_MODEL"
    ok "$PULL_MODEL pulled"
  fi

  # Also pull CPU model if we pulled the GPU model (CPU is the fallback)
  if [[ "$HAS_GPU" == true && "$PULL_MODEL" != "$CPU_MODEL" ]]; then
    if echo "$INSTALLED" | grep -qx "$CPU_MODEL"; then
      ok "$CPU_MODEL (CPU fallback) already present"
    else
      echo "  Pulling CPU fallback $CPU_MODEL..."
      ollama pull "$CPU_MODEL"
      ok "$CPU_MODEL pulled"
    fi
  fi
else
  warn "Skipping Ollama install + model pull (--skip-ollama)"
fi

# ── 7. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}  TokenRanger install complete${NC}"
echo ""
echo "  Compute:  $( [[ "$HAS_GPU" == true ]] && echo "GPU (full compression, $GPU_MODEL)" || echo "CPU (light compression, $CPU_MODEL)" )"
echo "  Venv:     $VENV_DIR"
[[ "$SKIP_BUILD" == false ]] && echo "  Plugin:   $PROJECT_ROOT/dist/index.js"
echo ""
echo "  Next steps:"
echo "    openclaw plugins enable tokenranger"
echo "    openclaw tokenranger setup"
echo "    openclaw gateway restart"
echo ""
echo "  Health check after setup:"
echo "    curl http://127.0.0.1:8100/health"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

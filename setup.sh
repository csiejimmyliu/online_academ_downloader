#!/usr/bin/env bash
set -euo pipefail

# --- Detect Python ---
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3 not found. Please install Python 3." >&2
  exit 1
fi

# --- Create venv ---
VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment at $VENV_DIR ..."
  "$PY" -m venv "$VENV_DIR"
fi

# --- Activate venv (in this shell if sourced) ---
ACTIVATE="$VENV_DIR/bin/activate"
if [ ! -f "$ACTIVATE" ]; then
  echo "Activate script not found at $ACTIVATE" >&2
  exit 1
fi

# If this script is sourced, activation persists; if executed, it only applies to this process.
# To persist activation, run:  source ./setup.sh
source "$ACTIVATE"
VENV_PY="$(command -v python)"
echo "Using virtualenv Python at: $VENV_PY"

# --- Ensure requirements.txt exists (optional default) ---
if [ ! -f requirements.txt ]; then
  echo "playwright>=1.45.0" > requirements.txt
  echo "Created default requirements.txt"
fi

# --- Install deps ---
echo "Installing Python dependencies..."
"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -r requirements.txt

echo "Installing Playwright Chromium..."
"$VENV_PY" -m playwright install chromium
# For Linux system deps (optional):
# "$VENV_PY" -m playwright install-deps

echo "✅ Setup complete."
if [[ "${BASH_SOURCE[0]-$0}" == "$0" ]]; then
  echo
  echo "To activate this virtualenv in your current shell, run:"
  echo "  source $ACTIVATE"
fi

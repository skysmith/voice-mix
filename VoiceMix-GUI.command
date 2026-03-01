#!/bin/bash
set -euo pipefail

cd /Users/sky/.openclaw/workspace/voice-mix

if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec python3 gui.py --dry "$@"

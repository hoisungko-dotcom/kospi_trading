#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: scripts/run_kr_ai_review.sh <YYYYMMDD> [--apply]"
  exit 1
fi

DATE_STR="$1"
APPLY_FLAG="${2:-}"

ROOT_DIR="/Users/hoisung/Downloads/turtle_trader_kis"
BOT_ROOT="${ROOT_DIR}/.tmp_kospi_pattern_bot"
PYTHON_BIN="${ROOT_DIR}/venv/bin/python"

CMD=("${PYTHON_BIN}" "${BOT_ROOT}/review/ai_reviewer.py" "--bot-root" "${BOT_ROOT}" "--date" "${DATE_STR}" "--reward-to-risk" "2.0")
if [[ "${APPLY_FLAG}" == "--apply" ]]; then
  CMD+=("--apply")
fi

"${CMD[@]}"

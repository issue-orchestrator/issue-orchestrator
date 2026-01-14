#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PATH="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON:-python3}"
PORT="${CC_PORT:-19080}"

ensure_venv() {
  if [[ ! -x "${VENV_PATH}/bin/python" ]]; then
    echo "Creating venv at ${VENV_PATH}"
    "${PYTHON_BIN}" -m venv "${VENV_PATH}"
  fi
}

ensure_deps() {
  if ! "${VENV_PATH}/bin/python" -c "import issue_orchestrator" >/dev/null 2>&1; then
    echo "Installing dev dependencies..."
    "${VENV_PATH}/bin/python" -m pip install -e ".[dev]"
  fi
}

ensure_venv
ensure_deps

exec "${VENV_PATH}/bin/python" -m issue_orchestrator.entrypoints.control_center --port "${PORT}" "$@"

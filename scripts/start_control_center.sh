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
  # Check if installed AND pointing to this repo (not a stale worktree)
  local installed_path
  installed_path=$("${VENV_PATH}/bin/python" -c "import issue_orchestrator; print(issue_orchestrator.__file__)" 2>/dev/null || echo "")

  if [[ -z "${installed_path}" || "${installed_path}" != "${ROOT_DIR}"/* ]]; then
    echo "Installing dev dependencies from ${ROOT_DIR}..."
    "${VENV_PATH}/bin/python" -m pip install -e ".[dev]"
  fi
}

ensure_venv
ensure_deps

exec "${VENV_PATH}/bin/python" -m issue_orchestrator.entrypoints.control_center --port "${PORT}" "$@"

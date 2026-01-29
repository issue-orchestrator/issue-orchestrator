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

  if [[ -z "${installed_path}" ]]; then
    echo "Package not installed, installing from ${ROOT_DIR}..."
    "${VENV_PATH}/bin/python" -m pip install -e ".[dev]"
  elif [[ "${installed_path}" != "${ROOT_DIR}"/* ]]; then
    echo "Stale install detected: ${installed_path}"
    echo "Reinstalling from ${ROOT_DIR}..."
    "${VENV_PATH}/bin/python" -m pip install -e ".[dev]"
  fi
}

ensure_port_free() {
  local pids attempt

  # Get all PIDs using the port (there may be multiple - parent/child)
  pids=$(lsof -ti :"${PORT}" 2>/dev/null || echo "")

  if [[ -z "${pids}" ]]; then
    return 0  # Port is free
  fi

  echo "Port ${PORT} is in use by PID(s): ${pids}"

  # Try graceful kill (SIGTERM) first
  for pid in ${pids}; do
    if ps -p "${pid}" -o comm= 2>/dev/null | grep -q -i python; then
      echo "Sending SIGTERM to old control center (PID ${pid})..."
      kill "${pid}" 2>/dev/null || true
    else
      echo "WARNING: PID ${pid} is not Python, skipping"
    fi
  done

  # Wait for port to be freed (up to 5 seconds)
  for attempt in {1..10}; do
    sleep 0.5
    if ! lsof -ti :"${PORT}" >/dev/null 2>&1; then
      echo "Port ${PORT} is now free"
      return 0
    fi
  done

  # SIGTERM didn't work, try SIGKILL on Python processes only
  echo "Graceful shutdown timed out, trying SIGKILL..."
  pids=$(lsof -ti :"${PORT}" 2>/dev/null || echo "")
  local killed_any=false
  for pid in ${pids}; do
    if ps -p "${pid}" -o comm= 2>/dev/null | grep -q -i python; then
      echo "Sending SIGKILL to PID ${pid}..."
      kill -9 "${pid}" 2>/dev/null || true
      killed_any=true
    fi
  done

  # Final wait
  sleep 1
  if lsof -ti :"${PORT}" >/dev/null 2>&1; then
    if [[ "${killed_any}" == "false" ]]; then
      echo "ERROR: Port ${PORT} is in use by non-Python process(es)"
    else
      echo "ERROR: Could not free port ${PORT} even with SIGKILL"
    fi
    echo "Please check what's using it: lsof -i :${PORT}"
    exit 1
  fi

  echo "Port ${PORT} is now free (required SIGKILL)"
}

show_startup_info() {
  local commit_sha
  commit_sha=$(cd "${ROOT_DIR}" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
  echo "Starting Control Center"
  echo "  Repo: ${ROOT_DIR}"
  echo "  Port: ${PORT}"
  echo "  Commit: ${commit_sha}"
}

ensure_venv
ensure_deps
ensure_port_free
show_startup_info

# Use unified entry point - it handles dashboard lifecycle
exec "${VENV_PATH}/bin/issue-orchestrator" "$@"

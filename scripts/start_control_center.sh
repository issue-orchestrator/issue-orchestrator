#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PATH="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON:-python3}"
PORT="${CC_PORT:-19080}"

# ---------------------------------------------------------------------------
# Stop all running orchestrator processes (control centers, agents, validators)
# ---------------------------------------------------------------------------
stop_all_orchestrators() {
  echo "=== Stopping all orchestrator processes ==="
  local found_any=false

  # 1. Control centers (any port)
  local cc_pids
  cc_pids=$(pgrep -f 'issue_orchestrator\.entrypoints\.control_center' 2>/dev/null || echo "")
  if [[ -n "${cc_pids}" ]]; then
    found_any=true
    local cc_count
    cc_count=$(echo "${cc_pids}" | wc -l | tr -d ' ')
    echo "  Killing ${cc_count} control center(s)..."
    echo "${cc_pids}" | xargs kill 2>/dev/null || true
  fi

  # 2. Claude agent sessions (orchestrator-spawned)
  local agent_pids
  agent_pids=$(pgrep -f 'claude.*--permission-mode bypassPermissions' 2>/dev/null || echo "")
  if [[ -n "${agent_pids}" ]]; then
    found_any=true
    local agent_count
    agent_count=$(echo "${agent_pids}" | wc -l | tr -d ' ')
    echo "  Killing ${agent_count} Claude agent session(s)..."
    echo "${agent_pids}" | xargs kill 2>/dev/null || true
  fi

  # 3. Validate runners
  local vr_pids
  vr_pids=$(pgrep -f 'issue_orchestrator\.entrypoints\.cli_tools\.validate_runner' 2>/dev/null || echo "")
  if [[ -n "${vr_pids}" ]]; then
    found_any=true
    local vr_count
    vr_count=$(echo "${vr_pids}" | wc -l | tr -d ' ')
    echo "  Killing ${vr_count} validate runner(s)..."
    echo "${vr_pids}" | xargs kill 2>/dev/null || true
  fi

  # 4. Playwright drivers (spawned by orchestrator)
  local pw_pids
  pw_pids=$(pgrep -f 'playwright/driver.*run-driver' 2>/dev/null || echo "")
  if [[ -n "${pw_pids}" ]]; then
    found_any=true
    echo "  Killing Playwright driver(s)..."
    echo "${pw_pids}" | xargs kill 2>/dev/null || true
  fi

  if [[ "${found_any}" == "false" ]]; then
    echo "  No orchestrator processes found"
    return 0
  fi

  # Wait for processes to exit (up to 5 seconds)
  echo "  Waiting for processes to exit..."
  local remaining
  for _ in {1..10}; do
    sleep 0.5
    remaining=$(pgrep -f 'issue_orchestrator\.entrypoints\.control_center|claude.*--permission-mode bypassPermissions|issue_orchestrator\.entrypoints\.cli_tools\.validate_runner' 2>/dev/null || echo "")
    if [[ -z "${remaining}" ]]; then
      echo "  All processes stopped"
      return 0
    fi
  done

  # Stragglers get SIGKILL
  echo "  Some processes didn't exit gracefully, sending SIGKILL..."
  echo "${remaining}" | xargs kill -9 2>/dev/null || true
  sleep 1

  remaining=$(pgrep -f 'issue_orchestrator\.entrypoints\.control_center|claude.*--permission-mode bypassPermissions|issue_orchestrator\.entrypoints\.cli_tools\.validate_runner' 2>/dev/null || echo "")
  if [[ -n "${remaining}" ]]; then
    echo "  WARNING: Some processes survived SIGKILL: ${remaining}"
  else
    echo "  All processes stopped (required SIGKILL)"
  fi
}

# ---------------------------------------------------------------------------
# Pull latest code into the base repo
# ---------------------------------------------------------------------------
git_pull() {
  echo "=== Pulling latest code ==="
  local current_branch
  current_branch=$(cd "${ROOT_DIR}" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")

  if [[ "${current_branch}" != "main" && "${current_branch}" != "master" ]]; then
    echo "ERROR: Base repo is on branch '${current_branch}', not main." >&2
    echo "  The orchestrator must run from latest main." >&2
    echo "  Fix: cd ${ROOT_DIR} && git checkout main" >&2
    exit 1
  fi

  if ! (cd "${ROOT_DIR}" && git diff --quiet && git diff --cached --quiet); then
    echo "ERROR: Base repo has uncommitted changes." >&2
    echo "  The orchestrator must run from a clean main." >&2
    echo "  Fix: cd ${ROOT_DIR} && git stash  (or git checkout .)" >&2
    exit 1
  fi

  if ! (cd "${ROOT_DIR}" && git pull --ff-only); then
    echo "ERROR: git pull --ff-only failed." >&2
    echo "  Local main has diverged from remote." >&2
    echo "  Fix: cd ${ROOT_DIR} && git reset --hard origin/main" >&2
    exit 1
  fi
}

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

is_our_process() {
  # Check if a PID belongs to the orchestrator (python, uvicorn, or entry point)
  local pid="$1"
  local cmdline
  cmdline=$(ps -p "${pid}" -o args= 2>/dev/null || echo "")
  if [[ -z "${cmdline}" ]]; then
    return 1  # Process already exited
  fi
  # Match python, uvicorn, or our entry point binary/module
  if echo "${cmdline}" | grep -q -i -E 'python|uvicorn|issue.orchestrator|issue-orchestrator'; then
    return 0
  fi
  return 1
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
    if is_our_process "${pid}"; then
      echo "Sending SIGTERM to old control center (PID ${pid})..."
      kill "${pid}" 2>/dev/null || true
    else
      echo "WARNING: PID ${pid} is not an orchestrator process, skipping"
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

  # SIGTERM didn't work, try SIGKILL
  echo "Graceful shutdown timed out, trying SIGKILL..."
  pids=$(lsof -ti :"${PORT}" 2>/dev/null || echo "")
  local killed_any=false
  for pid in ${pids}; do
    if is_our_process "${pid}"; then
      echo "Sending SIGKILL to PID ${pid}..."
      kill -9 "${pid}" 2>/dev/null || true
      killed_any=true
    fi
  done

  # Final wait
  sleep 1
  if lsof -ti :"${PORT}" >/dev/null 2>&1; then
    if [[ "${killed_any}" == "false" ]]; then
      echo "ERROR: Port ${PORT} is in use by non-orchestrator process(es)"
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
  echo "=== Starting Control Center ==="
  echo "  Repo: ${ROOT_DIR}"
  echo "  Port: ${PORT}"
  echo "  Commit: ${commit_sha}"
}

# --- Main ---
stop_all_orchestrators
# git_pull skipped — running from worktree for local dev
ensure_venv
ensure_deps
ensure_port_free
show_startup_info

# Use unified entry point - it handles dashboard lifecycle
# IO_DEV disables static file caching so CSS/JS changes are visible immediately
export IO_DEV=1
exec "${VENV_PATH}/bin/issue-orchestrator" "$@"

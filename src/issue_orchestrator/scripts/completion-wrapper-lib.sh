#!/bin/bash
# Shared completion-command wrapper resolution.

completion_candidate_paths=()

completion_wrapper_candidates() {
    local script_dir="$1"
    local command_name="$2"

    completion_candidate_paths=()

    if [[ -n "${ISSUE_ORCHESTRATOR_CC_REPO_ROOT:-}" ]]; then
        completion_candidate_paths+=("$ISSUE_ORCHESTRATOR_CC_REPO_ROOT/.venv/bin/$command_name")
    fi
    if [[ -n "${__PYVENV_LAUNCHER__:-}" ]]; then
        completion_candidate_paths+=("$(dirname "$__PYVENV_LAUNCHER__")/$command_name")
    fi

    # Normal source tree:
    #   .../issue-orchestrator/src/issue_orchestrator/scripts/
    # Snapshot source tree:
    #   .../issue-orchestrator/.control-center-snapshot/launch-*/src/issue_orchestrator/scripts/
    #
    # In the snapshot case the three-level parent is the launch directory, so
    # the explicit Control Center repo root / active venv candidates above
    # must win.
    local orchestrator_root
    orchestrator_root="$(dirname "$(dirname "$(dirname "$script_dir")")")"
    completion_candidate_paths+=("$orchestrator_root/.venv/bin/$command_name")
}

completion_wrapper_resolve() {
    local script_dir="$1"
    local command_name="$2"

    completion_wrapper_candidates "$script_dir" "$command_name"
    for candidate in "${completion_candidate_paths[@]}"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

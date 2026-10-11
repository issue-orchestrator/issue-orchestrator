"""Parse a PR head commit's status-check contexts into failed checks (#8692)."""

from __future__ import annotations

from typing import Any

from ...ports.pull_request_tracker import FailedCheck, FailedChecksRead

# A completed check run passes with one of these conclusions; any other
# completed conclusion (failure, timed_out, cancelled, startup_failure,
# action_required, stale) blocks merge, as the rollup itself counts it.
_PASSING_CHECK_RUN_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_FAILED_STATUS_STATES = frozenset({"FAILURE", "ERROR"})


def failed_checks_from_contexts(raw: dict[str, Any]) -> FailedChecksRead:
    """The failed contexts of ``get_failed_check_contexts``' raw answer."""
    checks: list[FailedCheck] = []
    for node in raw["contexts"]:
        if not isinstance(node, dict):
            continue
        kind = node.get("__typename")
        if kind == "CheckRun":
            failed = _failed_check_run(node)
        elif kind == "StatusContext":
            failed = _failed_status(node)
        else:
            failed = None
        if failed is not None:
            checks.append(failed)
    return FailedChecksRead(head_sha=raw["head_sha"], checks=tuple(checks))


def _failed_check_run(node: dict[str, Any]) -> FailedCheck | None:
    if node.get("status") != "COMPLETED":
        return None
    conclusion = str(node.get("conclusion") or "").upper()
    if conclusion in _PASSING_CHECK_RUN_CONCLUSIONS:
        return None
    run = ((node.get("checkSuite") or {}).get("workflowRun") or {}).get("databaseId")
    job = node.get("databaseId")
    actions_job = isinstance(run, int) and isinstance(job, int)
    return FailedCheck(
        name=str(node.get("name") or "check run"),
        conclusion=conclusion,
        required=bool(node.get("isRequired")),
        job_id=job if actions_job else None,
        run_id=run if actions_job else None,
    )


def _failed_status(node: dict[str, Any]) -> FailedCheck | None:
    state = str(node.get("state") or "").upper()
    if state not in _FAILED_STATUS_STATES:
        return None
    return FailedCheck(
        name=str(node.get("context") or "status"),
        conclusion=state,
        required=bool(node.get("isRequired")),
        job_id=None,
        run_id=None,
    )

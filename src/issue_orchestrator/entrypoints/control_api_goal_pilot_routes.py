"""Control Center Goal Pilot routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .control_api_goal_pilot_support import ControlApiGoalPilotDependency

control_goal_pilot_router = APIRouter()


def _not_initialized_response() -> JSONResponse:
    return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)


@control_goal_pilot_router.post("/control/goal_pilot/runs")
async def goal_pilot_create(
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return _not_initialized_response()
    body = await request.json()
    goals = body.get("goals") or []
    done_criteria = body.get("done_criteria") or {}
    name = body.get("name")
    milestones = body.get("milestones")
    if not name or not str(name).strip():
        return JSONResponse({"error": "name_required"}, status_code=400)
    pilot = deps.get_goal_pilot()
    run_id = pilot.create(goals=goals, done_criteria=done_criteria, name=name)
    if milestones:
        pilot.update_goals(run_id, goals, note=f"milestones={milestones}")
    return JSONResponse({"run_id": run_id})


@control_goal_pilot_router.get("/control/goal_pilot/runs")
async def goal_pilot_runs(deps: ControlApiGoalPilotDependency) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    pilot = deps.get_goal_pilot()
    return JSONResponse({"runs": pilot.list_runs()})


@control_goal_pilot_router.get("/control/goal_pilot/config")
async def goal_pilot_config(deps: ControlApiGoalPilotDependency) -> JSONResponse:
    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return _not_initialized_response()
    gp_config = orchestrator.config.goal_pilot
    configured = bool(gp_config.enabled and gp_config.agent)
    return JSONResponse({
        "enabled": gp_config.enabled,
        "agent": gp_config.agent,
        "approval_policy": gp_config.approval_policy,
        "approval_batch_size": gp_config.approval_batch_size,
        "approval_batch_window_minutes": gp_config.approval_batch_window_minutes,
        "configured": configured,
    })


@control_goal_pilot_router.get("/control/goal_pilot/runs/{run_id}")
async def goal_pilot_status(run_id: str, deps: ControlApiGoalPilotDependency) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    pilot = deps.get_goal_pilot()
    status = pilot.status(run_id)
    return JSONResponse({"status": status})


@control_goal_pilot_router.post("/control/goal_pilot/runs/{run_id}/phase")
async def goal_pilot_phase(
    run_id: str,
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    body = await request.json()
    phase = body.get("phase")
    reason = body.get("reason")
    changes = body.get("changes") or {}
    if not phase or not reason:
        return JSONResponse({"error": "phase_and_reason_required"}, status_code=400)
    pilot = deps.get_goal_pilot()
    result = pilot.set_phase(run_id, phase=phase, reason=reason, changes=changes)
    return JSONResponse(result)


@control_goal_pilot_router.get("/control/goal_pilot/runs/{run_id}/journeys")
async def goal_pilot_journeys(run_id: str, deps: ControlApiGoalPilotDependency) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    pilot = deps.get_goal_pilot()
    return JSONResponse({"journeys": pilot.list_journeys(run_id)})


@control_goal_pilot_router.post("/control/goal_pilot/runs/{run_id}/journeys")
async def goal_pilot_journey_create(
    run_id: str,
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    body = await request.json()
    pilot = deps.get_goal_pilot()
    try:
        journey = pilot.create_journey(run_id, body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"journey": journey})


@control_goal_pilot_router.patch("/control/goal_pilot/journeys/{journey_id}")
async def goal_pilot_journey_update(
    journey_id: str,
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    body = await request.json()
    pilot = deps.get_goal_pilot()
    try:
        journey = pilot.update_journey(journey_id, body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"journey": journey})


@control_goal_pilot_router.post("/control/goal_pilot/runs/{run_id}/journeys/reorder")
async def goal_pilot_journey_reorder(
    run_id: str,
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    body = await request.json()
    order = body.get("order")
    if not isinstance(order, list):
        return JSONResponse({"error": "order_list_required"}, status_code=400)
    pilot = deps.get_goal_pilot()
    try:
        result = pilot.reorder_journeys(run_id, order)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


@control_goal_pilot_router.patch("/control/goal_pilot/runs/{run_id}")
async def goal_pilot_update(
    run_id: str,
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    body = await request.json()
    goals = body.get("goals")
    note = body.get("note")
    if goals is None or not isinstance(goals, list):
        return JSONResponse({"error": "goals_required"}, status_code=400)
    pilot = deps.get_goal_pilot()
    result = pilot.update_goals(run_id, goals, note=note)
    return JSONResponse(result)


@control_goal_pilot_router.post("/control/goal_pilot/runs/{run_id}/actions")
async def goal_pilot_action(
    run_id: str,
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return _not_initialized_response()
    body = await request.json()
    action = body.get("action")
    if not isinstance(action, dict):
        return JSONResponse({"error": "action_required"}, status_code=400)
    pilot = deps.get_goal_pilot()
    result = pilot.execute_action(run_id, action, orchestrator.deps.repository_host)
    return JSONResponse(result)


@control_goal_pilot_router.get("/control/goal_pilot/skills")
async def goal_pilot_skills(
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    status = request.query_params.get("status")
    pilot = deps.get_goal_pilot()
    skills = pilot.list_skills(status=status)
    return JSONResponse({"skills": skills})


@control_goal_pilot_router.post("/control/goal_pilot/skills")
async def goal_pilot_upsert_skill(
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    body = await request.json()
    pilot = deps.get_goal_pilot()
    skill = pilot.upsert_skill(body)
    return JSONResponse({"skill": skill})


@control_goal_pilot_router.post("/control/goal_pilot/skills/export")
async def goal_pilot_export_skills(
    request: Request,
    deps: ControlApiGoalPilotDependency,
) -> JSONResponse:
    if deps.get_orchestrator() is None:
        return _not_initialized_response()
    body = await request.json()
    status = body.get("status", "active")
    pilot = deps.get_goal_pilot()
    result = pilot.export_skills(status=status)
    return JSONResponse(result)

"""The maintainer's ruling command on the engine's Control API (#8141).

``POST /api/issues/{n}/rulings`` records a maintainer's ruling on issue *n*
through the standing-rulings owner, which writes it into the issue body's
rulings block and the engine's index; every later agent prompt on the issue,
and every review of its PR, is then bound by it. ``DELETE
/api/issues/{n}/rulings/{id}`` retires one, ``GET /api/issues/{n}/rulings``
lists them.

These routes take the ADMIN token only (they are not on the agent-callback
allowlist): the bearer is the operator, which is what gives the ruling its
maintainer authority. An agent can never record one.
"""

from __future__ import annotations

import asyncio
import logging
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..control.standing_rulings import RetireOutcome
from ..domain.standing_ruling import RulingAuthority, RulingScope, maintainer_ruling_id
from ..ports.standing_rulings import StandingRulingsUnavailable
from .control_api_issue_support import ControlApiIssueDependency

if TYPE_CHECKING:
    from ..control.standing_rulings import StandingRulingsOwner

logger = logging.getLogger(__name__)

control_ruling_router = APIRouter()

#: Where a maintainer ruling recorded through this route says it came from.
MAINTAINER_RULING_SOURCE = "maintainer, through the engine's ruling command"


@dataclass(frozen=True)
class MaintainerRulingRequest:
    """The ruling a maintainer asks to record (untrusted JSON, validated here)."""

    text: str
    scope: RulingScope

    def content_token(self) -> str:
        """A stable 12-hex token of what is ruled: the same request is the same ruling."""
        content = {"text": self.text.strip(), "files": list(self.scope.files), "claims": list(self.scope.claims)}
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()[:12]

    @classmethod
    def from_wire(cls, raw: Any) -> "MaintainerRulingRequest":
        if not isinstance(raw, Mapping):
            raise ValueError("a ruling request is a JSON object")
        unknown = sorted(set(raw) - {"text", "files", "claims"})
        if unknown:
            raise ValueError(f"a ruling request takes text, files and claims only, not {unknown}")
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("a ruling request needs its text")
        files, claims = raw.get("files", []), raw.get("claims", [])
        if not isinstance(files, list) or not isinstance(claims, list):
            raise ValueError("a ruling's files and claims are lists")
        return cls(text=text, scope=RulingScope(files=tuple(files), claims=tuple(claims)))


def _owner(deps: ControlApiIssueDependency) -> "StandingRulingsOwner | None":
    orchestrator = deps.get_orchestrator()
    return None if orchestrator is None else orchestrator.deps.standing_rulings


def _not_ready() -> JSONResponse:
    return JSONResponse({"success": False, "error": "Orchestrator not initialized"}, status_code=503)


@control_ruling_router.get("/api/issues/{issue_number}/rulings")
async def list_rulings(issue_number: int, deps: ControlApiIssueDependency) -> JSONResponse:
    """The issue's standing rulings, as its body records them."""
    owner = _owner(deps)
    if owner is None:
        return _not_ready()
    try:
        rulings = await asyncio.to_thread(owner.active, issue_number)
    except StandingRulingsUnavailable as error:
        return JSONResponse({"success": False, "error": str(error)}, status_code=503)
    return JSONResponse({"success": True, "rulings": [ruling.to_dict() for ruling in rulings]})


@control_ruling_router.post("/api/issues/{issue_number}/rulings")
async def record_ruling(issue_number: int, request: Request, deps: ControlApiIssueDependency) -> JSONResponse:
    """Record a maintainer's ruling on the issue (module docstring)."""
    owner = _owner(deps)
    if owner is None:
        return _not_ready()
    try:
        wanted = MaintainerRulingRequest.from_wire(await request.json())
        ruling = owner.ruling(
            # Named by its content: a retried request records nothing twice.
            ruling_id=maintainer_ruling_id(wanted.content_token()),
            text=wanted.text,
            authority=RulingAuthority.MAINTAINER,
            source=MAINTAINER_RULING_SOURCE,
            scope=wanted.scope,
        )
    except ValueError as error:
        return JSONResponse({"success": False, "error": str(error)}, status_code=400)
    try:
        outcome = await asyncio.to_thread(owner.record, issue_number, ruling)
    except StandingRulingsUnavailable as error:
        return JSONResponse({"success": False, "error": str(error)}, status_code=409)
    except Exception as error:
        logger.exception("Recording a ruling on #%d failed", issue_number)
        return JSONResponse({"success": False, "error": str(error)}, status_code=500)
    return JSONResponse({
        "success": True, "issue_number": issue_number, "ruling_id": ruling.ruling_id, "outcome": outcome.value,
    })


@control_ruling_router.delete("/api/issues/{issue_number}/rulings/{ruling_id}")
async def retire_ruling(issue_number: int, ruling_id: str, deps: ControlApiIssueDependency) -> JSONResponse:
    """Retire a ruling: a maintainer's own act, like recording one."""
    owner = _owner(deps)
    if owner is None:
        return _not_ready()
    try:
        outcome = await asyncio.to_thread(owner.retire, issue_number, ruling_id)
    except StandingRulingsUnavailable as error:
        return JSONResponse({"success": False, "error": str(error)}, status_code=409)
    except Exception as error:
        logger.exception("Retiring ruling %s on #%d failed", ruling_id, issue_number)
        return JSONResponse({"success": False, "error": str(error)}, status_code=500)
    found = outcome is RetireOutcome.RETIRED
    return JSONResponse(
        {"success": found, "issue_number": issue_number, "ruling_id": ruling_id, "outcome": outcome.value},
        status_code=200 if found else 404,
    )

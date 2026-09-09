"""Receipt-based coding-done delivery; local candidate remains intact on failure."""

import base64
import os
from hashlib import sha256
from pathlib import Path
from urllib.request import Request

from ...contracts.ui_openapi_models import (
    CompletionIntakeReceiptPayload,
    CompletionSubmissionPayload,
)
from ...domain.completion_intake import CompletionIntakeReceipt
from .agent_callback import api_request_headers, resolve_control_api_port

CAPABILITY_ENV = "ISSUE_ORCHESTRATOR_COMPLETION_CAPABILITY"


def submission_payload(path: Path) -> CompletionSubmissionPayload:
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    # The same candidate bytes retain their retry key even if the client dies
    # before receiving an acknowledgement. Corrected bytes derive a new key.
    return CompletionSubmissionPayload(
        raw_bytes=base64.b64encode(raw).decode("ascii"),
        content_sha256=digest,
        submission_key=digest,
    )


def post_completion_command(route: str, payload: bytes, *, timeout: int) -> bytes:
    """Send only to the configured local owner, without credential-bearing redirects."""
    capability = os.environ.get(CAPABILITY_ENV)
    port = resolve_control_api_port()
    if not capability or not port:
        raise RuntimeError(
            "completion intake capability/endpoint unavailable; candidate preserved"
        )
    headers = api_request_headers().to_mutable_mapping()
    headers["X-Completion-Capability"] = capability
    request = Request(
        f"http://127.0.0.1:{int(port)}{route}",
        data=payload,
        headers=dict(headers),
        method="POST",
    )
    from urllib.request import HTTPRedirectHandler, build_opener

    class RefuseRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    with build_opener(RefuseRedirect()).open(request, timeout=timeout) as response:
        return response.read(1024 * 1024)


def submit_completion_file(path: Path) -> CompletionIntakeReceipt:
    payload = submission_payload(path)
    result = CompletionIntakeReceiptPayload.model_validate_json(
        post_completion_command(
            "/api/completion/submissions",
            payload.model_dump_json().encode(),
            timeout=30,
        )
    )
    if result.content_sha256 != payload.content_sha256:
        raise RuntimeError("completion intake returned a mismatched receipt")
    return CompletionIntakeReceipt(result.entry_id, result.content_sha256)

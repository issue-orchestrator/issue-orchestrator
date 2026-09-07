"""Real receipt/validation owners for subprocess exchange integration tests."""

import base64
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock
from uuid import uuid4

import pytest

from issue_orchestrator.contracts.ui_openapi_models import CompletionSubmissionPayload
from issue_orchestrator.control.completion_intake import CompletionEvidenceIntakeService
from issue_orchestrator.control.completion_intake_validation import (
    ConfiguredCompletionEvidenceValidator,
)
from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.domain.completion_intake import SubmitCompletionEvidence
from issue_orchestrator.domain.issue_run_allocation import IssueRunAllocation
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_tools import create_git
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.execution.historical_intake_custody import (
    IsolatedCompletionValidationWorkspace,
)
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports.background_job import BackgroundJobRunner
from issue_orchestrator.ports.historical_intake import HistoricalIntakeHandler


exchange_intake_context: ContextVar["ExchangeIntakeFixture"] = ContextVar(
    "exchange_intake"
)
TEST_CALLBACK_TOKEN = "test-agent-callback-token"


def serve_completion_submission(
    handler: BaseHTTPRequestHandler, owner: CompletionEvidenceIntakeService
) -> bool:
    """Subprocess HTTP transport; producer/real FastAPI contract is covered separately."""
    if handler.path != "/api/completion/submissions":
        return False
    if handler.headers.get("Authorization") != f"Bearer {TEST_CALLBACK_TOKEN}":
        handler.send_response(401)
        handler.end_headers()
        return True
    try:
        payload = CompletionSubmissionPayload.model_validate_json(
            handler.rfile.read(int(handler.headers["Content-Length"]))
        )
        receipt = owner.submit(
            handler.headers.get("X-Completion-Capability", ""),
            SubmitCompletionEvidence(
                base64.b64decode(payload.raw_bytes, validate=True),
                payload.content_sha256,
                payload.submission_key,
            ),
        )
    except ValueError:
        handler.send_response(422)
        handler.end_headers()
        return True
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(
        json.dumps(
            {"entry_id": receipt.entry_id, "content_sha256": receipt.content_sha256}
        ).encode()
    )
    return True


class ExchangeIntakeFixture:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        state = tmp_path / "receipt-owner"
        self.ledger = SqliteIssueRunLedger(state / "issue_run_ledger.sqlite")
        git = create_git(LocalCommandRunner())
        self.owner = CompletionEvidenceIntakeService(
            self.ledger,
            ConfiguredCompletionEvidenceValidator(
                GitWorkingCopy(git),
                LocalCommandRunner(),
                IsolatedCompletionValidationWorkspace(state, git),
                command="true",
                timeout_seconds=30,
            ),
            Mock(spec=HistoricalIntakeHandler),
            Mock(spec=BackgroundJobRunner),
        )
        owner = self.owner

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if not serve_completion_submission(self, owner):
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        monkeypatch.setenv("STUB_COMPLETION_API_PORT", str(self.server.server_port))
        monkeypatch.setenv(
            "ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN", TEST_CALLBACK_TOKEN
        )

    def allocator(self, output):
        return IssueRunAllocationService(output, self.ledger)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@dataclass(frozen=True)
class CodingCommandRun:
    environment: dict[str, str] = field(repr=False)
    run: SessionRunAssets
    owner: CompletionEvidenceIntakeService


@contextmanager
def coding_command_environment(worktree: Path, supplied: dict[str, str] | None):
    """Allocate a real contract-test run before handing authority to its CLI child."""
    state = worktree.parent / ("command-intake-" + uuid4().hex)
    with pytest.MonkeyPatch.context() as environment:
        with ExchangeIntakeFixture(state, environment) as fixture:
            run = fixture.allocator(FileSystemSessionOutput()).allocate(
                IssueRunAllocation(
                    worktree_path=worktree.resolve(),
                    session_name="issue-123",
                    issue_number=123,
                    session_key=SessionKey(
                        FakeIssueKey("123", "example/repo"), TaskKind.CODE
                    ),
                    agent_label="agent:contract",
                    backend="subprocess",
                )
            )
            child_env = {**os.environ, **(supplied or {})}
            child_env.update(
                ISSUE_ORCHESTRATOR_API_PORT=str(fixture.server.server_port),
                ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN=TEST_CALLBACK_TOKEN,
                ISSUE_ORCHESTRATOR_COMPLETION_CAPABILITY=fixture.owner.submission_capability(
                    run
                ),
                ISSUE_ORCHESTRATOR_RUN_DIR=str(run.run_dir),
                ISSUE_ORCHESTRATOR_SESSION_ID=run.session_name,
            )
            # The candidate locator is intentionally independent of receipt
            # authority. These older consumer tests inspect this exact file.
            child_env.setdefault(
                "ISSUE_ORCHESTRATOR_COMPLETION_PATH",
                ".issue-orchestrator/completion.json",
            )
            try:
                yield CodingCommandRun(child_env, run, fixture.owner)
            finally:
                fixture.owner.close_and_drain(123)

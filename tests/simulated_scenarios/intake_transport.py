"""Bind scripted agents to the composed engine's real completion intake owner."""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from issue_orchestrator.ports.agent_callback_endpoint import AgentCallbackEndpoint
from issue_orchestrator.ports.completion_intake import CompletionIntakeRuntime
from tests.integration.completion_intake_fixture import serve_completion_submission


scenario_transports: ContextVar[ExitStack] = ContextVar("scenario_intake_transports")


@contextmanager
def completion_transport(
    owner: CompletionIntakeRuntime,
    endpoint: AgentCallbackEndpoint,
    issue_numbers: tuple[int, ...],
):
    """Publish only a bound listener, retaining the same owner until final drain."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if not serve_completion_submission(self, owner):
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint.publish_bound_port(server.server_port)
    try:
        yield
    finally:
        try:
            for issue_number in issue_numbers:
                owner.close_and_drain(issue_number)
        finally:
            endpoint.declare_unavailable()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

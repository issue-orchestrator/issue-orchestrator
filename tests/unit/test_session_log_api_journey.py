"""Agent journey: fetch a session log via the dashboard API and read it.

The existing route tests in `test_web_session_log_routes.py` pin
plumbing — that the route returns 200 with the correct run's content
when `run_dir` is supplied, and 400 when it is not. They do **not**
pin what an agent or human debugging a session actually expects to see
when they fetch the log:

  - Multiple entries come back, not just one.
  - Entries are in file order (chronological), not shuffled.
  - All entries parsed cleanly — no `_parse_error: true` markers
    indicating the route silently swallowed malformed lines.
  - Both user-side and assistant-side messages are surfaced so the
    agent can distinguish prompts from responses.
  - The `limit` parameter actually limits, so a long-running session
    doesn't blow up the dashboard payload.
  - Each entry retains a `type` field — without it the agent can't
    tell what role the message played in the conversation.

This test sets up a realistic Claude-session.jsonl with five entries
covering the message types an agent expects (user prompt, assistant
text, assistant tool_use, user tool_result, assistant follow-up) and
exercises the production HTTP route via TestClient. If this test
fails, the gap is in `web_session_routes.get_claude_log_content` or
in the manifest-accessor's claude-log resolution — not in the test.
"""

from __future__ import annotations

import json
from pathlib import Path

# Reuse the test scaffolding (mock orchestrator, app fixture, set_orchestrator
# helper) so we get the same FastAPI app + dependency wiring everything else
# in this directory uses.
from tests.unit import test_web as _support  # noqa: F401
from tests.unit.test_web import *  # noqa: F401, F403  -- TestClient, app, etc.

from fastapi.testclient import TestClient

from issue_orchestrator.domain.models import SessionHistoryEntry
from issue_orchestrator.entrypoints import web
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


# A realistic Claude-session.jsonl excerpt. Five entries: user prompt,
# assistant thinking, assistant tool_use, user tool_result, assistant
# follow-up. This is the shape an agent reading the log expects to see.
_SESSION_LOG_LINES = [
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": "Fix the failing test in tests/unit/test_one.py",
        },
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll start by reading the failing test."}
            ],
        },
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "tests/unit/test_one.py"},
                }
            ],
        },
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "content": "def test_pass():\n    assert compute() == 1\n",
                }
            ],
        },
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Found it. The test asserts compute() == 1.",
                }
            ],
        },
    },
]


def _write_session_log(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


class TestSessionLogApiJourney:
    """End-to-end: dashboard hits /api/session/claude-log and gets a
    log an agent can actually read."""

    def test_log_is_readable_ordered_and_role_distinguishable(
        self, tmp_path: Path
    ) -> None:
        """The agent journey: fetch the session log → see ordered entries
        → distinguish user prompts from assistant responses → identify
        tool calls.

        Asserts what an agent debugging a stuck session needs:
          - 200 + non-empty entries.
          - File order preserved (entry[0] = first line written).
          - All entries parse — none has `_parse_error: true`.
          - Both user and assistant `type`s are present.
          - At least one assistant tool_use entry survives parsing
            (so the agent can see what the assistant DID, not just
            what it said).
          - `entry_count` matches `len(entries)` (the API tells the
            truth about how much it returned).
        """
        mock_orch = create_mock_orchestrator()  # noqa: F405
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-session-log"
        worktree.mkdir(parents=True)

        run = session_output.start_run(worktree, "coding-1", issue_number=5050)
        log_path = run.run_dir / "claude-session.jsonl"
        _write_session_log(log_path, _SESSION_LOG_LINES)
        session_output.update_manifest(
            run.run_dir, {"claude_log_path": str(log_path)}
        )

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=5050,
                title="Issue 5050",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=2,
                worktree_path=worktree,
            ),
        ]
        web.set_orchestrator(mock_orch)
        try:
            client = TestClient(web.app)
            response = client.get(
                f"/api/session/claude-log/5050?run_dir={run.run_dir}"
            )
            assert response.status_code == 200, response.text
            payload = response.json()

            entries = payload["entries"]
            assert len(entries) == len(_SESSION_LOG_LINES), (
                "Wrong number of entries surfaced. The API silently dropped "
                f"some lines: got {len(entries)}, expected "
                f"{len(_SESSION_LOG_LINES)}."
            )
            assert payload["entry_count"] == len(entries), (
                "entry_count disagrees with len(entries) — the API is "
                "lying about how many entries it returned."
            )

            # No silent parse failures. If this hits, the JSONL was
            # produced or transmitted in a way the route can't read,
            # and the agent sees garbage instead of structured turns.
            parse_errors = [e for e in entries if e.get("_parse_error")]
            assert not parse_errors, (
                f"{len(parse_errors)} entries failed to parse: "
                f"{parse_errors[:2]}"
            )

            # File order preserved. Without this the agent can't follow
            # the conversation. We check using the first message's text
            # marker which only appears at index 0.
            assert entries[0]["type"] == "user"
            assert "Fix the failing test" in str(entries[0]["message"]["content"])
            # And the last entry is the final assistant message.
            assert entries[-1]["type"] == "assistant"
            last_content = entries[-1]["message"]["content"]
            assert any(
                "Found it" in c.get("text", "")
                for c in last_content
                if isinstance(c, dict)
            ), "Last entry should be the assistant's final text response."

            # Role discrimination: both user prompts and assistant
            # responses are present, distinguishable by `type`.
            types = [e["type"] for e in entries]
            assert "user" in types and "assistant" in types, (
                "An agent can't distinguish prompts from responses — "
                f"types seen: {set(types)}"
            )

            # Tool calls reach the API: this is the entire reason the
            # agent fetches the log instead of just the prompt — to
            # see what the assistant DID.
            tool_use_entries = [
                e for e in entries
                if e["type"] == "assistant"
                and isinstance(e["message"]["content"], list)
                and any(
                    c.get("type") == "tool_use"
                    for c in e["message"]["content"]
                )
            ]
            assert tool_use_entries, (
                "No tool_use entries surfaced. The agent can see the "
                "assistant's text but not its actions."
            )
        finally:
            web.set_orchestrator(None)

    def test_limit_query_param_caps_returned_entries(
        self, tmp_path: Path
    ) -> None:
        """A long-running session can produce thousands of log entries.
        The `limit` query param must actually cap what the route returns,
        otherwise the dashboard payload blows up and the agent times out.
        """
        mock_orch = create_mock_orchestrator()  # noqa: F405
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-session-log-limit"
        worktree.mkdir(parents=True)

        run = session_output.start_run(worktree, "coding-1", issue_number=5051)
        log_path = run.run_dir / "claude-session.jsonl"
        # Write 50 entries; ask for 10.
        many_entries = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"step {i}"}],
                },
            }
            for i in range(50)
        ]
        _write_session_log(log_path, many_entries)
        session_output.update_manifest(
            run.run_dir, {"claude_log_path": str(log_path)}
        )

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=5051,
                title="Issue 5051",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
                worktree_path=worktree,
            ),
        ]
        web.set_orchestrator(mock_orch)
        try:
            client = TestClient(web.app)
            response = client.get(
                f"/api/session/claude-log/5051?run_dir={run.run_dir}&limit=10"
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert len(payload["entries"]) == 10, (
                f"limit=10 returned {len(payload['entries'])} entries"
            )
            # And it returns the first 10, not a tail / random slice.
            first_text = payload["entries"][0]["message"]["content"][0]["text"]
            assert first_text == "step 0", (
                f"Expected first 10 entries (step 0..9), got first={first_text}"
            )
        finally:
            web.set_orchestrator(None)

    def test_missing_run_dir_returns_400_not_a_silent_empty(
        self, tmp_path: Path
    ) -> None:
        """Sanity: when run_dir is omitted, the route must fail fast
        with 400 rather than silently returning an empty entries list.
        A silent empty would let a UI bug masquerade as a successful
        empty session.
        """
        mock_orch = create_mock_orchestrator()  # noqa: F405
        web.set_orchestrator(mock_orch)
        try:
            client = TestClient(web.app)
            response = client.get("/api/session/claude-log/5052")
            assert response.status_code == 400
            payload = response.json()
            assert "run_dir" in payload["error"]
        finally:
            web.set_orchestrator(None)

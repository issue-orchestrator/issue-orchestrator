"""Interaction helpers for persistent PTY sessions."""

from __future__ import annotations

import os
import shlex
from typing import Protocol

from .session_interactions import (
    SessionInteractionHandler,
    builtin_session_interaction_rules,
)


class _WritablePersistentSession(Protocol):
    master_fd: int
    closed: bool


def persistent_interaction_handler(
    command: list[str],
) -> SessionInteractionHandler | None:
    rules = builtin_session_interaction_rules(shlex.join(command))
    if not rules:
        return None
    label = command[0].rsplit("/", 1)[-1] if command else "persistent-agent"
    return SessionInteractionHandler(session_name=label, rules=rules)


def bind_interaction_sender(
    session: _WritablePersistentSession,
    handler: SessionInteractionHandler,
) -> None:
    handler.bind_sender(lambda text: _send_interaction_line(session, text))


def _send_interaction_line(session: _WritablePersistentSession, text: str) -> bool:
    if session.closed:
        return False
    payload = f"{text}\r".encode("utf-8")
    try:
        written = os.write(session.master_fd, payload)
    except (BlockingIOError, OSError):
        return False
    return written == len(payload)

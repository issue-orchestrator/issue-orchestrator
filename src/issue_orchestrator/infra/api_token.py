"""Bearer-token resolution for the Control API.

The Control API binds to 127.0.0.1 but is otherwise reachable from any
process running as the same user. Shared-secret tokens gate mutating
routes so that a stray local process (or a misconfigured non-loopback
bind) cannot issue ``/control/orchestrator/start``, request shutdowns,
subscribe to the SSE stream, etc.

Two tokens live here (see security #5987 F3 + #6017 reviews):

- **Admin token** — authorizes any Control API route. Held by the
  orchestrator process itself, the operator CLI, the Control Center,
  and MCP clients that the operator drives. Resolved from
  ``ISSUE_ORCHESTRATOR_API_TOKEN`` or ``~/.issue-orchestrator/api-token``.
- **Agent-callback token** — authorizes a narrow allowlist of routes
  (``/api/preflight-push`` and ``/api/issues/{n}/resume``) so agent
  subprocesses can trigger those flows without holding the admin
  credential. Resolved from ``ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN``
  or ``~/.issue-orchestrator/agent-callback-token``.

Both tokens are 32-byte hex strings (256 bits of entropy) and are
compared with ``hmac.compare_digest`` to avoid timing side channels.

Scope and limits
----------------

These tokens protect the Control API against:

- **Non-orchestrator same-user processes** (random scripts, browser
  extensions, other local daemons). They cannot discover the token
  without either inheriting the process env or reading the 0600
  file.
- **Cross-user attackers on a shared host.** File permissions +
  loopback binding stop them.
- **A future misconfigured non-loopback bind.** The token becomes
  the primary defense at that point.

They do **NOT** protect against:

- **A deliberately malicious agent running under the same user.**
  Agents launched by the orchestrator keep the real HOME
  (``terminal_subprocess.py`` sets ``isolate_home=False``), so
  ``~/.issue-orchestrator/api-token`` is directly readable from
  inside the agent. The agent-callback token is **defense in
  depth** — it narrows the default blast radius when the admin
  token has not been exfiltrated, and is the right shape for a
  future isolated-agent model — but it is not a hard privilege
  boundary today. Achieving that requires OS-level isolation
  (separate user, container, or sandbox profile). Tracked as
  issue #6024.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "ISSUE_ORCHESTRATOR_API_TOKEN"
AGENT_CALLBACK_TOKEN_ENV_VAR = "ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN"

_DEFAULT_ADMIN_RELATIVE = Path(".issue-orchestrator") / "api-token"
_DEFAULT_AGENT_RELATIVE = Path(".issue-orchestrator") / "agent-callback-token"


def default_token_path() -> Path:
    """Return the default on-disk location for the admin Control API token."""
    return Path.home() / _DEFAULT_ADMIN_RELATIVE


def default_agent_callback_token_path() -> Path:
    """Return the default on-disk location for the agent-callback token."""
    return Path.home() / _DEFAULT_AGENT_RELATIVE


def generate_token() -> str:
    """Generate a fresh 32-byte hex token (256 bits)."""
    return secrets.token_hex(32)


def _load_token_file(path: Path) -> str | None:
    """Read a token from ``path`` if present.

    Returns ``None`` if the file does not exist or is empty. Does NOT
    auto-create — reserved for ``load_or_create_token``.
    """
    if not path.exists():
        return None
    try:
        token = path.read_text().strip()
    except OSError as exc:
        logger.debug("Could not read token file %s: %s", path, exc)
        return None
    if not token:
        return None
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        logger.warning(
            "Token file %s has permissions %o (expected 0600). Tightening.",
            path,
            mode,
        )
        try:
            path.chmod(0o600)
        except OSError as exc:
            logger.warning("Could not tighten permissions on %s: %s", path, exc)
    return token


def load_or_create_token(path: Path | None = None) -> str:
    """Load the token from ``path``, generating it if missing.

    On creation the file is written atomically with mode ``0600``. On
    read, non-0600 permissions are logged as a warning but still honored
    to avoid locking operators out of a running orchestrator because of
    an incidental chmod.
    """
    resolved = path or default_token_path()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = _load_token_file(resolved)
    if existing:
        return existing
    if resolved.exists():
        logger.warning(
            "Control API token file %s was empty; regenerating.", resolved
        )

    token = generate_token()
    tmp = resolved.with_suffix(resolved.suffix + ".tmp")
    tmp.write_text(token)
    tmp.chmod(0o600)
    tmp.replace(resolved)
    logger.info("Generated Control API token at %s", resolved)
    return token


def read_existing_token(path: Path | None = None) -> str | None:
    """Read the admin token from disk if it already exists; do not create.

    Used by client-side helpers (CLI, MCP, Control Center probes) that
    need to authenticate to an already-running orchestrator without
    side-effecting the filesystem for users who have never started
    one. See security #6017 P3 review.
    """
    return _load_token_file(path or default_token_path())


def resolve_api_token(path: Path | None = None) -> str:
    """Return the admin Control API token, generating the file if absent.

    The env var wins when set (tests, operator overrides). Otherwise the
    on-disk file is loaded — or created on first use for the server
    startup path.
    """
    from_env = os.environ.get(TOKEN_ENV_VAR)
    if from_env:
        return from_env
    return load_or_create_token(path)


def resolve_agent_callback_token(path: Path | None = None) -> str:
    """Return the agent-callback token, generating the file if absent."""
    from_env = os.environ.get(AGENT_CALLBACK_TOKEN_ENV_VAR)
    if from_env:
        return from_env
    return load_or_create_token(path or default_agent_callback_token_path())


def read_existing_admin_token() -> str | None:
    """Return the admin token from env or disk, without creating the file."""
    from_env = os.environ.get(TOKEN_ENV_VAR)
    if from_env:
        return from_env
    return read_existing_token()


def read_existing_agent_callback_token() -> str | None:
    """Return the agent-callback token from env or disk, without creating it."""
    from_env = os.environ.get(AGENT_CALLBACK_TOKEN_ENV_VAR)
    if from_env:
        return from_env
    return _load_token_file(default_agent_callback_token_path())


def verify_token(expected: str, provided: str | None) -> bool:
    """Constant-time comparison of bearer token values."""
    if not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))

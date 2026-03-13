"""Client-facing URL resolution for local and remote UI surfaces."""

from __future__ import annotations

import os


def _normalize_local_host(host: str) -> str:
    normalized = host.strip()
    if normalized in {"", "0.0.0.0", "::", "[::]"}:
        return "localhost"
    return normalized


def _codespaces_forwarding_domain() -> tuple[str, str] | None:
    codespace_name = os.environ.get("CODESPACE_NAME", "").strip()
    forwarding_domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "").strip()
    if not codespace_name or not forwarding_domain:
        return None
    return codespace_name, forwarding_domain


def resolve_client_base_url(port: int, *, local_host: str = "127.0.0.1") -> str:
    """Resolve a browser-usable base URL for a local dashboard port."""
    if port <= 0:
        msg = f"Port must be positive, got {port}"
        raise ValueError(msg)

    forwarded = _codespaces_forwarding_domain()
    if forwarded is not None:
        codespace_name, forwarding_domain = forwarded
        return f"https://{codespace_name}-{port}.{forwarding_domain}"

    return f"http://{_normalize_local_host(local_host)}:{port}"


def resolve_client_dashboard_url(port: int, *, local_host: str = "127.0.0.1") -> str:
    """Resolve the browser-usable dashboard URL for a dashboard port."""
    return resolve_client_base_url(port, local_host=local_host).rstrip("/") + "/"

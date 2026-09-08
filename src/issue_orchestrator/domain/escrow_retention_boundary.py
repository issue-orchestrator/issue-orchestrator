"""Reserved durable escrow namespace, excluded from unrelated maintenance."""

from pathlib import Path


ESCROW_PATH_PARTS = (".issue-orchestrator", "state", "validated-work")


def is_escrow_path(path: Path) -> bool:
    for candidate in (path.absolute(), path.resolve()):
        parts = candidate.parts
        if any(parts[i : i + 3] == ESCROW_PATH_PARTS for i in range(len(parts) - 2)):
            return True
    return False


def require_disposable_path(path: Path) -> None:
    if is_escrow_path(path):
        raise ValueError(
            "validated-work escrow belongs to resolved-record retention only"
        )

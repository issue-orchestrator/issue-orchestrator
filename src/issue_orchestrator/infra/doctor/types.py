"""Shared types for doctor diagnostics."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Check:
    """A single diagnostic check result."""
    name: str
    status: str  # "ok", "warning", "error", "info"
    detail: str


@dataclass
class DoctorResult:
    """Result of running diagnostics."""
    checks: list[Check] = field(default_factory=list)

    @property
    def overall(self) -> str:
        """Overall status based on all checks."""
        if any(c.status == "error" for c in self.checks):
            return "error"
        if any(c.status == "warning" for c in self.checks):
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "overall": self.overall,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail}
                for c in self.checks
            ],
        }

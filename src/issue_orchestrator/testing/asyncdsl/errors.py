from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass
class WaitTimeout(Exception):
    message: str
    diagnostics: dict[str, Any]

    def __str__(self) -> str:
        return f"{self.message}\nDiagnostics: {self.diagnostics}"

@dataclass
class NoProgressTimeout(Exception):
    message: str
    diagnostics: dict[str, Any]

    def __str__(self) -> str:
        return f"{self.message}\nDiagnostics: {self.diagnostics}"

@dataclass
class EventGapDetected(Exception):
    expected_next: int
    received: int
    last_seen: int

    def __str__(self) -> str:
        return f"Event gap detected: expected {self.expected_next}, got {self.received} (last_seen={self.last_seen})"

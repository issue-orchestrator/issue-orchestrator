"""Shared pattern-registry records, sized the way real ones are (#7272).

The defect these fixtures exist for is a size cliff, so a fixture that is
comfortable is a fixture that proves nothing. The density here is taken from the
registry that actually broke: `porchpin/porchpin` held 53 patterns in 104 787
bytes, or roughly 1 977 bytes per entry, almost all of it accumulated
observation identities.
"""

from __future__ import annotations

from issue_orchestrator.adapters.github.pattern_registry import format_entries
from issue_orchestrator.ports.pattern_registry import (
    CaseFileClassification,
    PatternRegistryEntry,
)

#: Measured on porchpin's registry, 2026-09-17: 104 787 B across 53 patterns.
PORCHPIN_PATTERN_COUNT = 53
PORCHPIN_BYTES_PER_PATTERN = 104787 / 53

#: Chosen so a record of N patterns lands within a few percent of that density.
OBSERVATIONS_PER_PATTERN = 34


def registry_record(pattern_count: int) -> str:
    """A registry of ``pattern_count`` committed patterns, as it is stored."""
    entries = {}
    for index in range(pattern_count):
        signature = f"stranded-validated-work-{index:04d}"
        entries[signature] = PatternRegistryEntry(
            signature=signature,
            reservation_id=f"reservation-{index:04d}",
            claimant_id="engine-a",
            expires_at="2026-09-17T12:00:00+00:00",
            pending=None,
            issue_number=6000 + index,
            observation_ids=tuple(
                f"run-20260917-{index:04d}:session-tech-lead-{index:04d}:A{seq:02d}"
                for seq in range(OBSERVATIONS_PER_PATTERN)
            ),
            classification=CaseFileClassification(),
        )
    return format_entries(entries)

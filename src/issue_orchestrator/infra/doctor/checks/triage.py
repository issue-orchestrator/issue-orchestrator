"""Triage workflow label checks for doctor (#6779 R3).

When triage is configured, act-level proposals are filed as GitHub issues
carrying the ``proposed-triage`` gate. A fresh install that never provisioned
that label would create ungated (schedulable) proposal issues, so surface the
missing gate here in addition to the applier's fail-before-create guard.
"""

from typing import TYPE_CHECKING

from ..types import Check

if TYPE_CHECKING:
    from ...config import Config


def check_triage_labels(config: "Config | None" = None) -> list[Check]:
    if config is None or not config.triage_review_agent or not config.repo:
        return []  # triage/repo not configured -> nothing to verify

    from ....domain.triage_session import PROPOSED_TRIAGE_LABEL

    try:
        from ....execution.providers import create_repository_host

        host = create_repository_host(repo=config.repo, config=config)
        existing = {
            name.casefold()
            for entry in host.list_labels()
            if isinstance(entry, dict) and isinstance((name := entry.get("name")), str)
        }
    except Exception as exc:
        # Advisory only: a GitHub read failure must not fail doctor outright
        # (auth/connectivity are covered by their own checks).
        return [
            Check(
                name="Triage Labels",
                status="warning",
                detail=f"Could not verify the '{PROPOSED_TRIAGE_LABEL}' gate label: {exc}",
            )
        ]

    gate_present = PROPOSED_TRIAGE_LABEL.casefold() in existing
    if gate_present:
        return [
            Check(
                name="Triage Labels",
                status="ok",
                detail=f"Gate label '{PROPOSED_TRIAGE_LABEL}' provisioned",
            )
        ]
    return [
        Check(
            name="Triage Labels",
            status="error",
            detail=(
                f"Gate label '{PROPOSED_TRIAGE_LABEL}' is missing — triage"
                " proposals would be ungated. Run `issue-orchestrator init`."
            ),
        )
    ]

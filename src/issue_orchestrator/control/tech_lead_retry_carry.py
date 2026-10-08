"""What a tech-lead validation retry carries into its new run (#7273, #8347).

Two things, checked together before the retry takes its durable work claim: the
original run's launch authority and inputs (carried, never re-derived), and the
rulings of the work that authority covers (read fresh, since a ruling recorded
or retired since the original launch binds the retry).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..ports.repository_host import RepositoryHostError
from ..ports.standing_rulings import StandingRulingsUnavailable
from .session_launch_types import LaunchResult
from .tech_lead_covered_rulings import retried_covered_rulings
from .tech_lead_session_policy import carry_launch_authority_forward

if TYPE_CHECKING:
    from ..domain.models import PendingValidationRetry
    from ..domain.session_run import SessionRunAssets
    from ..ports.launch_prompt import LaunchPromptProvider
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .tech_lead_run_inputs import LaunchAuthorityTransfer


@dataclass(frozen=True)
class CarriedRetry:
    """The retry's carried authority (None for a retry that is no tech-lead
    run's) and the rulings of the work it covers (None when none bind it)."""

    transfer: "LaunchAuthorityTransfer | None"
    covered_rulings: str | None


def carry_retry(
    *,
    tech_lead_authority: "TechLeadAuthorityStore",
    launch_prompt: "LaunchPromptProvider",
    retry: "PendingValidationRetry",
    run: "SessionRunAssets",
) -> CarriedRetry | LaunchResult:
    """The carried retry, or the launch's refusal: a lost authority refuses for
    good; rulings that cannot be read now keep the retry queued (a GitHub rate
    limit as a deferral), and the caller releases its claim either way."""
    carried = carry_launch_authority_forward(tech_lead_authority, retry, run)
    if isinstance(carried, str):
        return LaunchResult(None, False, carried)
    try:
        covered = retried_covered_rulings(launch_prompt, carried)
    except StandingRulingsUnavailable as error:
        return LaunchResult.required_input_unavailable(str(error))
    except RepositoryHostError as error:  # a rate limit stays a deferral
        return LaunchResult.input_preparation_failed("Covered work's standing rulings", error)
    return CarriedRetry(carried, covered)

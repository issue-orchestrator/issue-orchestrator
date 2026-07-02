from __future__ import annotations

import pytest

from issue_orchestrator.control.review_routing import should_queue_pr_review


def _decide(**overrides) -> bool:
    kwargs = dict(
        has_pr=True,
        code_review_agent_configured=True,
        skip_review=False,
        is_review_session=False,
        review_exchange_completed=False,
        review_exchange_halted=False,
    )
    kwargs.update(overrides)
    return should_queue_pr_review(**kwargs)


def test_queues_review_for_completed_pr_with_agent_configured() -> None:
    assert _decide() is True


@pytest.mark.parametrize(
    "override",
    [
        {"has_pr": False},
        {"code_review_agent_configured": False},
        {"skip_review": True},
        {"is_review_session": True},
        {"review_exchange_completed": True},
        {"review_exchange_halted": True},
    ],
)
def test_suppresses_review_for_each_distinction(override) -> None:
    assert _decide(**override) is False

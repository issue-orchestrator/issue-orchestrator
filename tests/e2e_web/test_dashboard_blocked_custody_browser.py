"""Blocked-item custody in a real browser against a real dashboard (#7331).

The engine behind the page is a real custody reader over real in-memory state:
a proposal waiting in the op ledger, a tech-lead escalation label, and an item
nobody owns. The page must show each one's custody in words, the Blocked
column's attention count, a keyboard-operable "Why this state?" disclosure
that survives a refresh, and no page errors, in both themes.

Set ``CUSTODY_SCREENSHOT_DIR`` to also save light and dark screenshots of the
Blocked column (review evidence, not an assertion).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import Page, expect

from issue_orchestrator.control.blocked_item_custody_reader import (
    StateBlockedItemCustodyReader,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_charter import CharterAuthority, TechLeadCharter, decide_charter
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    TechLeadCharterDecision,
    decision_key,
)
from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp
from issue_orchestrator.entrypoints import web as web_module
from issue_orchestrator.ports.blocked_item_custody import NO_ACTION_LIVENESS_OWNER
from issue_orchestrator.ports.provider_resilience import StaticProviderCircuitStatusReader
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from tests.e2e_web.conftest import (
    FlowWebMockOrchestrator,
    UvicornTestServer,
    _configure_flow_deps,
    find_free_port,
)

PROPOSED, ESCALATED, NOBODY = 501, 502, 503


def _authority() -> InMemoryTechLeadAuthorityStore:
    now = datetime.now(timezone.utc)
    authority = InMemoryTechLeadAuthorityStore()
    authority.record_op(
        issue_number=700,
        op=StoredTechLeadOp(
            op_type="kill_hung_session",
            target_issue_number=PROPOSED,
            rationale="hung",
            source_run_id="run-1",
            source_session_name=f"issue-{PROPOSED}",
            source_action_id="A1",
            created_at=(now - timedelta(hours=2)).isoformat(),
            target_session_id="s",
            target_terminal_id="t",
            target_session_type="issue",
        ),
    )
    verdict = decide_charter(
        "kill_hung_session",
        TechLeadCharter.default(),
        action_ceiling=CharterAuthority.PROPOSE,
        ceiling_source="tech_lead.authority.kill_hung_session",
    )
    authority.charter_ledger.record_decisions(
        [
            TechLeadCharterDecision.from_verdict(
                verdict,
                decision_id=decision_key("run-1", "A1"),
                source=CharterDecisionSource.DECISION,
                run_id="run-1",
                action_id="A1",
                anchor_issue_number=PROPOSED,
                target_number=PROPOSED,
                target_is_pr=False,
                decided_at=(now - timedelta(hours=2)).isoformat(),
                tracks_proposal=True,
                proposal_issue_number=700,
            )
        ]
    )
    return authority


@pytest.fixture(scope="module")
def custody_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    orchestrator = FlowWebMockOrchestrator()
    _configure_flow_deps(orchestrator, tmp_path_factory.mktemp("custody-repo"))
    orchestrator.state.cached_scope_issues = [
        Issue(number=PROPOSED, title="Hung session proposal", labels=["agent:web", "blocked-failed"]),
        Issue(
            number=ESCALATED,
            title="Escalated by the tech lead",
            labels=["agent:web", "needs-human", "tech-lead-needs-human"],
        ),
        Issue(number=NOBODY, title="Nobody has looked", labels=["agent:web", "blocked-failed"]),
    ]
    orchestrator.blocked_item_custody = StateBlockedItemCustodyReader(
        config=orchestrator.config,
        state=lambda: orchestrator.state,
        labels=LabelManager(orchestrator.config),
        authority=_authority(),
        needs_human_causes=lambda numbers: {number: frozenset() for number in numbers},
        provider_lanes=lambda _agent: (),
        provider_circuits=StaticProviderCircuitStatusReader(),
        parked_actions=NO_ACTION_LIVENESS_OWNER,
        clock=lambda: datetime.now(timezone.utc),
    )
    web_module.configure_dashboard_admin_token(None)
    original = web_module.get_orchestrator()
    web_module.set_orchestrator(orchestrator)
    server = UvicornTestServer("127.0.0.1", find_free_port())
    server.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.config.port}"}
    finally:
        server.stop()
        web_module.set_orchestrator(original)


def _open(page: Page, url: str, theme: str = "dark") -> None:
    page.goto(f"{url}/?theme={theme}", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_function("() => window.dashboardBundleLoaded === true", timeout=15_000)


def _card(page: Page, number: int):  # type: ignore[no-untyped-def]
    return page.locator(f'[data-column="blocked"] .issue-card[data-issue="{number}"]')


def test_each_blocked_card_says_who_owns_it_and_the_header_counts_attention(
    page: Page, custody_server: dict[str, object]
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    _open(page, str(custody_server["url"]))

    expect(_card(page, PROPOSED).locator(".custody-label")).to_have_text("Waiting on you")
    expect(_card(page, ESCALATED).locator(".custody-label")).to_have_text("Waiting on you")
    expect(_card(page, NOBODY).locator(".custody-label")).to_have_text("Unowned")
    expect(_card(page, NOBODY).locator(".custody-attention")).to_have_text("Needs attention")
    summary = page.locator('[data-column="blocked"] .blocked-custody-summary')
    expect(summary).to_have_attribute("role", "status")
    expect(summary.locator(".custody-summary-count")).to_have_text("1 needs attention")
    assert errors == []


def test_why_opens_from_the_keyboard_and_survives_a_refresh(
    page: Page, custody_server: dict[str, object]
) -> None:
    _open(page, str(custody_server["url"]))
    why = _card(page, PROPOSED).locator("details.custody-why")
    summary = why.locator("summary")

    summary.focus()
    expect(summary).to_be_focused()
    page.keyboard.press("Enter")

    expect(why).to_have_attribute("open", "")
    expect(why.locator(".custody-reason")).to_contain_text("Proposal #700 (kill hung session) awaits your approval")
    expect(why.locator(".custody-charter")).to_contain_text("Proposed, awaiting approval")
    expect(why.locator(".custody-charter")).to_contain_text("tech_lead.authority.kill_hung_session")

    page.evaluate("() => refreshViewModel({ reloadOnListChange: false })")
    expect(why).to_have_attribute("open", "")  # an unchanged card is reused, not rebuilt


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_custody_renders_in_both_themes_without_clipping(
    page: Page, custody_server: dict[str, object], theme: str
) -> None:
    page.set_viewport_size({"width": 1400, "height": 1000})
    _open(page, str(custody_server["url"]), theme)
    expect(page.locator("html")).to_have_attribute("data-theme", theme)
    card = _card(page, PROPOSED)
    card.locator("details.custody-why summary").click()

    for selector in (".custody-reason", ".custody-charter", ".custody-label"):
        box = card.locator(selector).evaluate(
            "el => ({scroll: el.scrollWidth, client: el.clientWidth})"
        )
        assert box["scroll"] <= box["client"] + 1, f"{selector} clips in {theme}"

    out = os.environ.get("CUSTODY_SCREENSHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.locator('[data-column="blocked"]').screenshot(
            path=str(Path(out) / f"blocked-custody-{theme}.png")
        )

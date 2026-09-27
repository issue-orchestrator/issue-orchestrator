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


def _set_labels(number: int, labels: list[str]) -> None:
    orchestrator = web_module.get_orchestrator()
    orchestrator.state.cached_scope_issues = [
        Issue(number=issue.number, title=issue.title, labels=labels) if issue.number == number else issue
        for issue in orchestrator.state.cached_scope_issues
    ]


def _refresh(page: Page) -> None:
    page.evaluate("() => refreshViewModel({ reloadOnListChange: false })")


@pytest.mark.parametrize("surface", ["compact", "expanded"])
def test_an_open_why_keeps_its_state_and_focus_when_the_custody_changes(
    page: Page, custody_server: dict[str, object], surface: str
) -> None:
    _open(page, str(custody_server["url"]))
    if surface == "compact":
        root = page.locator('[data-column="blocked"] .column-cards')
        row = root.locator(f'.issue-card[data-issue="{NOBODY}"]')
    else:
        page.evaluate("() => toggleColumnExpand('blocked')")
        root = page.locator('[data-column="blocked"] .expanded-cards-list')
        row = root.locator(f'.expanded-card[data-issue="{NOBODY}"]')
    summary = row.locator("details.custody-why > summary")
    expect(summary).to_be_visible()
    summary.focus()
    page.keyboard.press("Enter")
    expect(row.locator("details.custody-why")).to_have_attribute("open", "")

    _set_labels(NOBODY, ["agent:web", "needs-human"])  # Unowned -> Waiting on you
    try:
        _refresh(page)
        expect(row.locator(".custody-label")).to_have_text("Waiting on you")  # rebuilt
        expect(row.locator("details.custody-why")).to_have_attribute("open", "")
        expect(row.locator("details.custody-why > summary")).to_be_focused()
    finally:
        _set_labels(NOBODY, ["agent:web", "blocked-failed"])
        _refresh(page)


_CONTRAST = """
(el) => {
    // WCAG contrast of el's text against what is actually painted behind it:
    // semi-transparent backgrounds are composited up to the first opaque one,
    // and every ancestor's opacity fades the text.
    const rgba = (c) => {
        // "rgb(a)(r, g, b[, a])", or "color(srgb r g b[ / a])" (0-1 channels)
        // as color-mix() backgrounds compute.
        const v = (c.match(/[\\d.]+/g) || []).map(Number);
        const channels = c.startsWith('color(') ? [...v.slice(0, 3).map((x) => x * 255), ...v.slice(3)] : v;
        return channels.length === 3 ? [...channels, 1] : channels;
    };
    const lum = ([r, g, b]) => {
        const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const over = (top, under) => top.slice(0, 3).map((v, i) => v * top[3] + under[i] * (1 - top[3]));
    const layers = [];
    let opacity = 1;
    for (let n = el; n; n = n.parentElement) {
        const style = getComputedStyle(n);
        opacity *= Number(style.opacity);
        const bg = rgba(style.backgroundColor);
        if (bg.length === 4 && bg[3] > 0) layers.push(bg);
    }
    let painted = [255, 255, 255];
    const html = rgba(getComputedStyle(document.documentElement).backgroundColor);
    if (html[3] > 0) painted = over(html, painted);
    for (const layer of layers.reverse()) painted = over(layer, painted);
    const fg = rgba(getComputedStyle(el).color);
    const text = over([...fg.slice(0, 3), fg[3] * opacity], painted);
    const [a, b] = [lum(text) + 0.05, lum(painted) + 0.05];
    return Math.max(a, b) / Math.min(a, b);
}
"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_custody_text_on_a_viewed_row_stays_readable(
    page: Page, custody_server: dict[str, object], theme: str
) -> None:
    _open(page, str(custody_server["url"]), theme)
    page.evaluate("() => toggleColumnExpand('blocked')")
    row = page.locator(f'[data-column="blocked"] .expanded-card[data-issue="{PROPOSED}"]')
    expect(row).to_be_visible()
    row.evaluate("el => el.classList.add('viewed')")

    for selector in (".custody-label", ".custody-age", "details.custody-why > summary"):
        ratio = row.locator(selector).evaluate(_CONTRAST)
        assert ratio >= 4.5, f"{selector} contrast {ratio:.2f} in {theme}"
    unowned = page.locator(f'[data-column="blocked"] .expanded-card[data-issue="{NOBODY}"]')
    ratio = unowned.locator(".custody-attention").evaluate(_CONTRAST)
    assert ratio >= 4.5, f"attention pill contrast {ratio:.2f} in {theme}"
    header = page.locator('[data-column="blocked"] .blocked-custody-summary')
    for selector in (".custody-summary-count", ".custody-summary-headline", ".custody-summary-states li"):
        ratio = header.locator(selector).first.evaluate(_CONTRAST)
        assert ratio >= 4.5, f"summary {selector} contrast {ratio:.2f} in {theme}"

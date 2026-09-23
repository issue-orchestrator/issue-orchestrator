"""Real dashboard approval controls: keyboard focus and narrow layout."""

import json

import pytest

from playwright.sync_api import Page, expect


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_scoped_rework_keyboard_and_narrow_layout(
    page: Page, web_server: dict, theme: str
) -> None:
    proposal = {
        "proposal_issue_number": 501,
        "repository": "porchpin/porchpin",
        "pr_number": 94,
        "issue_number": 5,
        "expected_head": "a" * 40,
        "evidence_identity": "b" * 64,
        "feedback": "Actor-scope public reads",
        "report": "T1 evidence " + "x" * 300,
        "status": "awaiting_approval",
        "detail": "Awaiting approval",
        "mutations": "Preserve branch; invalidate reviewed labels and queue rework",
        "can_approve": True,
        "can_decline": True,
        "forward_issue_number": 0,
    }
    commands = []

    def respond(route):
        if route.request.method == "POST":
            commands.append(route.request.post_data_json)
            proposal.update(status="approved", can_approve=False)
            result = {
                "outcome": "approved",
                "detail": "Approval recorded",
                "proposal_issue_number": 501,
            }
        else:
            result = {"proposals": [proposal]}
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(result)
        )

    page.route("**/api/tech-lead/rework-proposals", respond)
    page.emulate_media(color_scheme=theme)
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(str(web_server["url"]), wait_until="domcontentloaded")
    summary = page.locator("#reworkProposalsSummary")
    expect(summary).to_be_visible()
    summary.focus()
    expect(summary).to_be_focused()
    summary.press("Enter")
    approve = page.get_by_role("button", name="Approve rework for PR 94")
    expect(approve).to_be_visible()
    report = page.get_by_text("Review report", exact=True)
    report.focus()
    report.press("Enter")
    report.press("Tab")
    expect(approve).to_be_focused()
    assert approve.evaluate("e => getComputedStyle(e).outlineStyle") != "none"
    for node in [approve, page.locator(".rework-proposal")]:
        box = node.bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 375
    approve.press("Enter")
    expect(page.locator("#reworkProposalStatus")).to_have_text("Approval recorded")
    expect(summary).to_be_focused()
    assert commands == [{"proposal_issue_number": 501, "decision": "approve"}]


def test_expanded_blocked_column_keeps_rework_proposals_reachable(
    page: Page, web_server: dict
) -> None:
    proposal = {
        "proposal_issue_number": 501,
        "repository": "porchpin/porchpin",
        "pr_number": 94,
        "issue_number": 5,
        "expected_head": "a" * 40,
        "evidence_identity": "b" * 64,
        "feedback": "Review the scoped change",
        "report": "Evidence " * 100,
        "status": "awaiting_approval",
        "detail": "Awaiting approval",
        "mutations": "Preserve branch and queue rework",
        "can_approve": True,
        "can_decline": True,
        "forward_issue_number": 0,
    }
    page.route(
        "**/api/tech-lead/rework-proposals",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"proposals": [proposal]}),
        ),
    )
    page.set_viewport_size({"width": 1000, "height": 600})
    page.goto(str(web_server["url"]), wait_until="domcontentloaded")
    page.locator('[data-column="blocked"] .column-expand-btn').click()
    expect(page.locator("body")).to_have_class("column-focus-mode")
    summary = page.locator("#reworkProposalsSummary")
    summary.evaluate("element => element.click()")
    expect(page.locator(".rework-proposal")).to_be_visible()
    page.get_by_text("Review report", exact=True).click()
    summary.evaluate("element => element.scrollIntoView({block: 'start'})")
    assert page.locator(".rework-proposal-actions").evaluate(
        "element => element.getBoundingClientRect().bottom > innerHeight"
    )
    page.mouse.move(500, 300)
    page.mouse.wheel(0, 700)
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    assert page.locator(".rework-proposal-actions").evaluate(
        "element => element.getBoundingClientRect().bottom <= innerHeight"
    )

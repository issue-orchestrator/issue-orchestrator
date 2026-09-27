"""Blocked-item custody on the rendered dashboard (#7331): UI guardrails.

The custody owner decides every state; these pin how the page renders it:
the first paint (Jinja) and the client rebuild (``blocked_custody.js``) say
the same thing, the disclosure is a native control that is never nested in
another control, the header count is a live region, colour is never the only
signal, and nothing clips. The payload -> markup unit cases live in
``tests/js/blocked_custody_render.test.js``; the producer half is
``tests/unit/test_blocked_custody_projection.py``.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest
from bs4 import BeautifulSoup

from issue_orchestrator.domain.blocked_item_custody import (
    BlockedCustodyBoard,
    BlockedItemCustody,
    CustodyCharterBasis,
    CustodyClock,
    CustodyState,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.entrypoints.web_templates import get_templates
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.provider_resilience import NO_PROVIDER_CIRCUIT_STATUS
from issue_orchestrator.ports.tech_lead_run_record_store import NO_TECH_LEAD_RUN_HISTORY
from issue_orchestrator.view_models.blocked_custody import (
    custody_payload,
    custody_summary_payload,
)
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from issue_orchestrator.view_models.dashboard_assets import DASHBOARD_JS_CHUNKS

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "issue_orchestrator" / "static"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

BASIS = CustodyCharterBasis(
    decision_id="decision:run-1:A1",
    run_id="run-1",
    action_kind="kill_hung_session",
    role="flow",
    required_depth="workaround",
    role_enabled=False,
    role_depth="restructure",
    role_authority="execute",
    action_ceiling="propose",
    ceiling_source="tech_lead.authority.kill_hung_session",
    outcome="proposed",
    reason_code="action_authority_propose",
    reason="flow may act, but <kill_hung_session> is set to 'propose'",
    decided_at=(NOW - timedelta(hours=1)).isoformat(),
    lifecycle="awaiting_approval",
    proposal_issue_number=700,
)


def _custody(number: int, state: CustodyState, **overrides: Any) -> BlockedItemCustody:
    fields: dict[str, Any] = {
        "issue_number": number,
        "state": state,
        "reason": f"Reason for #{number} & \"quotes\"",
        "clock": CustodyClock(since=NOW - timedelta(hours=3), basis="session started"),
        "stale_after": None if state is CustodyState.UNOWNED else timedelta(hours=2),
        "stale": False,
    }
    fields.update(overrides)
    return BlockedItemCustody(**fields)


CASES = [
    _custody(1, CustodyState.WAITING_ON_YOU, charter=BASIS, stale=True),
    _custody(2, CustodyState.UNOWNED, clock=None),
    _custody(3, CustodyState.HELD, clock=CustodyClock(since=NOW - timedelta(hours=1), basis="last issue activity", lower_bound=True)),
]


def _js(function: str, argument: dict[str, Any]) -> str:
    runner = textwrap.dedent(
        f"""
        const vm = require('node:vm');
        const fs = require('node:fs');
        const escapeHtml = (v) => String(v).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
        const context = {{ escapeHtml, escapeAttr: escapeHtml }};
        vm.createContext(context);
        vm.runInContext(fs.readFileSync({json.dumps(str(STATIC / 'js' / 'dashboard' / 'blocked_custody.js'))}, 'utf8'), context);
        process.stdout.write(context.{function}(JSON.parse(process.argv[1])));
        """
    )
    result = subprocess.run(
        ["node", "-e", runner, json.dumps(argument)], check=True, capture_output=True, text=True
    )
    return result.stdout


def _jinja(macro: str, argument: dict[str, Any]) -> str:
    module = get_templates().get_template("_blocked_custody.html").module
    return str(getattr(module, macro)(argument))


def _normalized(markup: str) -> str:
    """Jinja and the JS escaper spell a quote differently (&#34; vs &quot;)."""
    return html.unescape(markup)


@pytest.mark.parametrize("custody", CASES, ids=lambda c: c.state.value)
def test_first_paint_and_client_rebuild_render_the_same_custody(custody: BlockedItemCustody) -> None:
    payload = custody_payload(custody, NOW).model_dump(mode="json")

    jinja = _jinja("custody_block", payload)
    js = _js("renderCustodyHtml", {"custody": payload})

    assert jinja  # a parity check of two empty strings proves nothing
    assert _normalized(jinja) == _normalized(js)


def test_first_paint_and_client_rebuild_render_the_same_summary_and_key() -> None:
    board = BlockedCustodyBoard(items=tuple(CASES))
    summary = custody_summary_payload(board).model_dump(mode="json")

    assert _normalized(_jinja("custody_summary", summary)) == _normalized(
        _js("renderBlockedCustodySummaryHtml", summary)
    )
    assert _jinja("custody_summary_key", summary) == _js("blockedCustodySummaryKey", summary)


# -- the rendered dashboard ---------------------------------------------------------


@dataclass
class _Orchestrator:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


@dataclass
class _Reader:
    board: BlockedCustodyBoard

    def read(self, issue_numbers: Sequence[int]) -> BlockedCustodyBoard:
        return BlockedCustodyBoard(items=tuple(self.board.for_issue(n) for n in issue_numbers))


def _view_model():  # type: ignore[no-untyped-def]
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.e2e.enabled = False
    state = OrchestratorState(
        startup_status="complete",
        cached_scope_issues=[
            Issue(number=n, title=f"Blocked {n}", labels=["agent:web", "blocked-failed"])
            for n in (1, 2, 3)
        ],
    )
    return build_dashboard_view_model(
        _Orchestrator(state=state, config=config),
        provider_circuit=NO_PROVIDER_CIRCUIT_STATUS,
        tech_lead_history=NO_TECH_LEAD_RUN_HISTORY,
        blocked_custody=_Reader(BlockedCustodyBoard(items=tuple(CASES))),
        active_tab="blocked",
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )


def _dashboard() -> BeautifulSoup:
    view_model = _view_model()
    page = get_templates().get_template("dashboard.html").render(**view_model.template_context())
    return BeautifulSoup(page, "html.parser")


def test_every_blocked_card_shows_its_custody_on_first_paint() -> None:
    soup = _dashboard()
    column = soup.select_one('[data-column="blocked"]')
    assert column is not None

    states = [node["data-custody-state"] for node in column.select(".issue-card .card-custody")]
    assert states == ["waiting_on_you", "unowned", "held"]
    labels = [node.get_text() for node in column.select(".issue-card .custody-label")]
    assert labels == ["Waiting on you", "Unowned", "Held"]
    for other in soup.select('[data-column]:not([data-column="blocked"]) .card-custody'):
        raise AssertionError(f"custody rendered outside the Blocked lane: {other}")


def test_the_blocked_header_is_a_live_count_in_words() -> None:
    soup = _dashboard()
    summary = soup.select_one('[data-column="blocked"] .blocked-custody-summary')
    assert summary is not None

    assert summary["role"] == "status"
    assert summary["aria-live"] == "polite"
    assert summary.select_one(".custody-summary-count").get_text() == "2 need attention"
    assert summary.select_one(".custody-summary-headline").get_text().startswith("2 of 3 blocked items")
    assert summary["data-custody-key"].startswith("3|2|1|1|")
    assert summary.name == "div"  # it holds a <ul>, which a <p> may not


def test_why_is_a_native_disclosure_never_nested_in_another_control() -> None:
    soup = _dashboard()
    disclosures = soup.select(".custody-why")
    assert disclosures

    for details in disclosures:
        assert details.name == "details"
        summary = details.find("summary", recursive=False)
        assert summary is not None and summary.get_text() == "Why this state?"
        for ancestor in details.parents:
            assert ancestor.name not in {"button", "a", "summary"}
            assert ancestor.get("role") != "button", "a disclosure inside role=button"


def test_the_list_row_renders_custody_beside_its_row_not_inside_it() -> None:
    view_model = _view_model()
    template = get_templates().get_template("issue_row.html")
    for issue in view_model.blocked_items:
        soup = BeautifulSoup(template.render(issue=issue, active_tab="blocked"), "html.parser")
        row = soup.select_one(".issue-row")
        custody = soup.select_one(".issue-row-custody .card-custody")
        assert row is not None and custody is not None
        assert row.select_one(".card-custody") is None
        assert custody["data-custody-state"] == issue["custody"]["state"]


def test_the_state_is_never_signalled_by_colour_alone() -> None:
    for custody in CASES:
        payload = custody_payload(custody, NOW).model_dump(mode="json")
        soup = BeautifulSoup(_jinja("custody_block", payload), "html.parser")
        assert soup.select_one(".custody-label").get_text() == custody.state.label
        icon = soup.select_one(".custody-icon")
        assert icon["aria-hidden"] == "true"
        if custody.needs_attention:
            assert soup.select_one(".custody-attention").get_text() == "Needs attention"


def _custody_css() -> str:
    css = (STATIC / "css" / "dashboard" / "cards.css").read_text()
    start = css.index("/* Blocked-item custody (#7331)")
    return re.sub(r"/\*.*?\*/", "", css[start:], flags=re.S)


def test_custody_styles_use_theme_variables_and_never_clip() -> None:
    css = _custody_css()

    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "colours come from theme variables"
    for clipping in ("nowrap", "overflow: hidden", "text-overflow", "max-height"):
        assert clipping not in css
    assert ".custody-why > summary:focus-visible" in css
    assert "outline:" in css.split(".custody-why > summary:focus-visible", 1)[1].split("}", 1)[0]


def test_the_renderer_loads_before_the_kanban_that_calls_it() -> None:
    chunks = list(DASHBOARD_JS_CHUNKS)
    assert chunks.index("blocked_custody.js") < chunks.index("kanban_columns.js")
    assert chunks.index("core.js") < chunks.index("blocked_custody.js")  # escapeHtml


def test_the_client_never_derives_a_custody_state() -> None:
    """The browser renders ``custody``; only the server owner decides it."""
    source = (STATIC / "js" / "dashboard" / "blocked_custody.js").read_text()
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))

    for derived in ("needs_human", "orchestrator_labels", "blocked_summary", "stale_after_minutes"):
        assert derived not in code
    assert re.search(r"custody\.state\s*===", code) is None


def _kanban_source() -> str:
    return (STATIC / "js" / "dashboard" / "kanban_columns.js").read_text()


def test_a_reused_compact_card_syncs_its_custody_age_in_place() -> None:
    """The fingerprint leaves the age out, so the reuse path must refresh it."""
    source = _kanban_source()
    reuse = source.split("syncCompactCardPhaseAge(existing, card);", 1)[1].split("}", 1)[0]

    assert "syncCustodyAge(existing, card);" in reuse


def test_the_expanded_column_refresh_is_contract_validated() -> None:
    source = _kanban_source()
    body = source.split("async function loadExpandedColumn", 1)[1].split("\nfunction ", 1)[0]

    assert "uiContractJson.fromResponse(resp, 'DashboardViewModelPayload', endpoint)" in body
    assert "resp.json()" not in body


def test_an_unchanged_expanded_list_still_syncs_custody_ages() -> None:
    body = _kanban_source().split("async function loadExpandedColumn", 1)[1].split("\nfunction ", 1)[0]
    unchanged = body.split("} else {", 1)[1].split("}", 1)[0]

    assert "syncExpandedCustodyAges(expandedList, items);" in unchanged


@pytest.mark.parametrize(
    ("path", "function"),
    [
        ("dashboard/kanban_columns.js", "function renderCompactCards"),
        ("dashboard/kanban_columns.js", "async function loadExpandedColumn"),
        ("dashboard/core.js", "async function refreshIssueRows"),
    ],
)
def test_every_surface_that_replaces_custody_keeps_its_open_disclosure(path: str, function: str) -> None:
    """One helper pair brackets every replacement of custody markup."""
    source = (STATIC / "js" / path).read_text()
    body = source.split(function, 1)[1].split("\nfunction ", 1)[0].split("\nasync function ", 1)[0]

    capture = body.index("captureCustodyDisclosures(")
    restore = body.index("restoreCustodyDisclosures(")
    assert capture < restore

"""Response-channel prompt policy for persistent review exchange."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from ..domain.review_artifacts import NitPolicy
from ..domain.review_exchange_turn import Role

ResponseChannel: TypeAlias = Literal["file", "mailbox"]


@dataclass(frozen=True)
class ReviewExchangeResponseChannels:
    """Response transport selected for each persistent exchange role."""

    coder: ResponseChannel = "mailbox"
    reviewer: ResponseChannel = "mailbox"

    @classmethod
    def file_only(cls) -> "ReviewExchangeResponseChannels":
        return cls(coder="file", reviewer="file")

    def for_role(self, role: Role) -> ResponseChannel:
        if role is Role.CODER:
            return self.coder
        if role is Role.REVIEWER:
            return self.reviewer
        raise ValueError(f"unknown review-exchange role: {role!r}")


_BOOTSTRAP_PROMPT_TEMPLATE = (
    "You are the {role} in a coder↔reviewer review exchange for issue "
    "#{issue_number}: {issue_title}.\n\n"
    "Wait for the orchestrator to send your role-specific instructions via "
    "stdin. The orchestrator may send the full prompt directly, or it may "
    "send a short notice pointing at a prompt file in this worktree. This "
    "bootstrap setup message is not a turn: do not write to "
    "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE until a turn arrives via stdin. "
    "For each turn, read the full instructions and follow them. Reviewers also "
    "write the human-readable report to $ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE. "
    "{submission_summary} "
    "Then wait for the next prompt. Do not exit on your own; the orchestrator "
    "will terminate you when the exchange is done.\n"
)


def build_bootstrap_prompt(
    *,
    role: Role,
    issue_number: int,
    issue_title: str,
    response_channel: ResponseChannel,
    response_file: Path,
) -> str:
    """Build the role bootstrap prompt for the selected response channel."""
    return _BOOTSTRAP_PROMPT_TEMPLATE.format(
        role=role.value,
        issue_number=issue_number,
        issue_title=issue_title,
        submission_summary=_bootstrap_submission_summary(
            response_channel=response_channel,
            response_file=response_file,
        ),
    )


def _bootstrap_submission_summary(
    *,
    response_channel: ResponseChannel,
    response_file: Path,
) -> str:
    if response_channel == "mailbox":
        return (
            "Each role submits its verdict for the turn by running the "
            "`exchange-respond` command; never write a response file unless a "
            "later turn prompt explicitly says this role is in file-channel mode."
        )
    return (
        "This role is in file-channel mode because loopback Control API "
        f"callbacks are unavailable. Submit each verdict by writing JSON to "
        f"$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE ({response_file}); do not "
        "run `exchange-respond`."
    )


def prompt_for_response_channel(
    prompt_text: str,
    *,
    role: Role,
    response_channel: ResponseChannel,
    response_file: Path,
) -> str:
    """Append channel-specific response submission instructions to a turn prompt."""
    return (
        prompt_text.rstrip()
        + "\n\n"
        + _channel_submission_override(
            role=role,
            response_channel=response_channel,
            response_file=response_file,
        )
    )


def _channel_submission_override(
    *,
    role: Role,
    response_channel: ResponseChannel,
    response_file: Path,
) -> str:
    if response_channel == "mailbox":
        return (
            "Response submission for this turn:\n"
            "- Run `exchange-respond <ok|changes_requested|disagree> --text \"...\"`.\n"
            "- Do not write to $ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE; it is "
            "routing metadata for the callback.\n"
        )
    if role is Role.REVIEWER:
        side_effect = (
            "- First write the markdown review report to "
            "$ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE.\n"
        )
        payload_hint = (
            "- The JSON object must include `response_type`, `response_text`, "
            "`getting_closer`, and the `decision` object described in the "
            "review artifact contract above.\n"
        )
    else:
        payload = (
            '{"response_type":"ok","response_text":"Applied the requested fixes."}'
        )
        side_effect = "- First run `coding-done completed ...` successfully.\n"
        payload_hint = f"- Example payload: `{payload}`\n"
    return (
        "Response submission override for this turn:\n"
        "- This role is using the file channel because loopback callbacks are "
        "not available in its sandbox.\n"
        f"{side_effect}"
        "- Do not run `exchange-respond` for this turn.\n"
        "- Write exactly one JSON object to "
        f"$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE ({response_file}).\n"
        f"{payload_hint}"
    )


def build_prompt_inbox_notice(
    *,
    role: Role,
    round_index: int,
    attempt_index: int,
    prompt_path: Path,
    response_channel: ResponseChannel,
) -> str:
    """Build the short PTY notice that points an agent at the full prompt file."""
    if response_channel == "mailbox":
        submission = (
            "Follow that file exactly, then submit your verdict by running "
            "`exchange-respond <ok|changes_requested|disagree> --text \"...\"`; "
            "never write a response file."
        )
    else:
        submission = (
            "Follow that file exactly, then submit your verdict by writing JSON "
            "to $ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE; "
            "do not run `exchange-respond`."
        )
    return (
        f"Review-exchange {role.value} turn round={round_index} "
        f"attempt={attempt_index} is ready.\n"
        f"Read the full instructions from: {prompt_path}\n"
        f"{submission}"
    )


def reviewer_prompt_with_artifact_contract(
    prompt: str, *, nit_policy: NitPolicy
) -> str:
    """Append the reviewer artifact contract to a reviewer turn prompt."""
    return (
        prompt.rstrip() + "\n\n"
        "Review artifact contract:\n"
        "- Write a human-readable markdown review to "
        "$ISSUE_ORCHESTRATOR_REVIEW_REPORT_FILE.\n"
        "- Submit your verdict by running `exchange-respond "
        "<ok|changes_requested|disagree> --text \"...\" "
        "--decision-json '{...}'`; the response-file env var is routing "
        "metadata, not an output path.\n"
        "- The `--decision-json` object must include: "
        "verdict, risk, blocking_findings, nits, tests_reviewed, "
        "abstraction_review, nit_policy.\n"
        "- Put review content in markdown; JSON item entries may be ID-only.\n"
        "- Use stable IDs (`F1`, `F2`, `N1`, ...), and include every JSON ID "
        "as a heading or bullet in the markdown report.\n"
        "- Include `abstraction_review` with `status` set to `no_issues`, "
        "`changes_requested`, or `deferred`. Use `A1`, `A2`, ... findings "
        "when a bounded owner/port/command abstraction should be added. "
        "Use `deferred` only with `follow_up_issue_url`.\n"
        "- `approved` decisions must not include blocking findings. "
        "`approved` decisions must not carry "
        "`abstraction_review.status=changes_requested`.\n"
        "- Review for the strongest bounded design, not merely for a working diff.\n"
        f"- The active nit policy for this coder is `{nit_policy}`. "
        "Classify nits honestly; the orchestrator decides whether to route them "
        "back to the coder before PR creation.\n"
        "- If the active policy is `address`, approved-with-nits is still an "
        "`approved` decision in your JSON; the orchestrator will route those "
        "nits through coder rework before PR creation.\n"
        "\n"
        "Example command:\n"
        "exchange-respond ok --getting-closer --text \"Looks good.\" "
        f"--decision-json '{{\"verdict\":\"approved\",\"risk\":\"low\","
        "\"blocking_findings\":[],\"nits\":[],\"tests_reviewed\":[\"pytest tests/unit -q\"],"
        "\"abstraction_review\":{\"status\":\"no_issues\",\"findings\":[]},"
        f"\"nit_policy\":\"{nit_policy}\"}}'"
        "\n"
    )

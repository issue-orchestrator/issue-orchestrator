"""``exchange-respond`` CLI — the single verb a review-exchange agent calls.

Replaces the freehand write to ``$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE``.
The agent composes its verdict once and hands it to this command; the
command delivers it to the orchestrator over the in-process Control API,
which binds it to the turn it currently has open (see ``TurnMailbox``).
The agent never names the turn, echoes a token, or picks a path — turn
correlation is decided server-side, so a slip-up degrades to an
orchestrator timeout/retry, never a silently-accepted wrong verdict.

Usage::

    exchange-respond ok --text "Applied the fixes."
    exchange-respond changes_requested --text "See F1." --decision-json '{...}'
    exchange-respond disagree --not-getting-closer --text "Wrong approach because…"
    exchange-respond --json '{"response_type":"ok","response_text":"…"}'

Exit codes: 0 = delivered (accepted); 1 = rejected by the orchestrator or
transport/usage error. A non-zero exit is informational — the orchestrator
drives correctness through the open slot regardless.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from .orchestrator_resume import api_request_headers

_VALID_RESPONSE_TYPES = ("ok", "changes_requested", "disagree")


def _routing_key() -> str | None:
    """The opaque per-role routing key the orchestrator opened the slot under.

    Reuses the response-file path the agent already has in its environment.
    Nothing is written to or read from that path — it is used purely as a
    stable, per-role identifier both sides already agree on.
    """
    key = os.environ.get("ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE")
    return key.strip() if key else None


def _api_port() -> str | None:
    port = os.environ.get("ISSUE_ORCHESTRATOR_API_PORT") or os.environ.get(
        "ORCHESTRATOR_API_PORT"
    )
    return port.strip() if port else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exchange-respond",
        description=(
            "Submit a review-exchange turn verdict to the orchestrator."
        ),
    )
    parser.add_argument(
        "response_type",
        nargs="?",
        choices=_VALID_RESPONSE_TYPES,
        help="The verdict for this turn.",
    )
    parser.add_argument("--text", help="Human-readable response text.")
    parser.add_argument(
        "--getting-closer",
        dest="getting_closer",
        action="store_true",
        default=None,
        help="Mark that the exchange is converging.",
    )
    parser.add_argument(
        "--not-getting-closer",
        dest="getting_closer",
        action="store_false",
        help="Mark that the exchange is not converging.",
    )
    parser.add_argument(
        "--decision-json",
        help="Reviewer's structured decision object as a JSON string.",
    )
    parser.add_argument(
        "--json",
        dest="full_json",
        help=(
            "Complete verdict payload as a JSON object. Overrides the "
            "positional/flag form; use for full fidelity."
        ),
    )
    return parser


def build_payload(args: argparse.Namespace) -> dict[str, object]:
    """Construct the verdict payload the orchestrator's parser consumes.

    Mirrors the JSON shape agents previously wrote to the response file:
    ``{response_type, response_text, [getting_closer], [decision]}``.
    """
    if args.full_json is not None:
        try:
            payload = json.loads(args.full_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--json is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("--json must be a JSON object")
        return payload

    if args.response_type is None:
        raise ValueError("response_type is required (or pass --json)")
    if not args.text or not args.text.strip():
        raise ValueError("--text is required")

    payload: dict[str, object] = {
        "response_type": args.response_type,
        "response_text": args.text,
    }
    if args.getting_closer is not None:
        payload["getting_closer"] = args.getting_closer
    if args.decision_json is not None:
        try:
            decision = json.loads(args.decision_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--decision-json is not valid JSON: {exc}") from exc
        if not isinstance(decision, dict):
            raise ValueError("--decision-json must be a JSON object")
        payload["decision"] = decision
    return payload


def _deliver(key: str, port: str, payload: dict[str, object]) -> tuple[bool, str]:
    """POST the verdict to the Control API. Returns (accepted, message)."""
    url = f"http://localhost:{port}/api/review-exchange/respond"
    body = json.dumps({"key": key, "payload": payload}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=api_request_headers().to_mutable_mapping(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, f"orchestrator rejected the verdict (HTTP {exc.code})"
    except urllib.error.URLError as exc:
        return False, f"could not reach orchestrator Control API: {exc}"
    status = result.get("status")
    if status == "accepted":
        return True, "verdict delivered"
    return False, f"verdict not accepted: {status} ({result.get('detail', '')})"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = build_payload(args)
    except ValueError as exc:
        print(f"exchange-respond: {exc}", file=sys.stderr)
        return 1

    key = _routing_key()
    port = _api_port()
    if not key:
        print(
            "exchange-respond: ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE not set; "
            "are you running under the orchestrator?",
            file=sys.stderr,
        )
        return 1
    if not port:
        print(
            "exchange-respond: ISSUE_ORCHESTRATOR_API_PORT not set; "
            "are you running under the orchestrator?",
            file=sys.stderr,
        )
        return 1

    accepted, message = _deliver(key, port, payload)
    if accepted:
        print(f"exchange-respond: {message}")
        return 0
    print(f"exchange-respond: {message}", file=sys.stderr)
    return 1


def safe_main() -> None:
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        # Last-resort guard for an agent CLI: never crash without a message.
        print(f"exchange-respond: internal error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    safe_main()

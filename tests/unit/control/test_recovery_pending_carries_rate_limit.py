"""Every recovery pending result built from a caught error carries its rate limit.

Recovery operations catch their own errors and return a pending result
(#7387) rather than re-raising, so claims, leases and the disposition gate
exit through their tested paths. The price is that each catch site must
attach ``rate_limit=host_rate_limit_of(error)``: a site that forgets turns a
GitHub rate limit into an ordinary failure that spends retry budget (the
class #7303 threaded through site by site). This guard makes forgetting a
test failure instead of a silent regression.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "issue_orchestrator"


def _pending_results_built_in_handlers() -> list[tuple[str, int, bool]]:
    found: list[tuple[str, int, bool]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            for node in ast.walk(handler):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name != "RecoveryAttemptPending":
                    continue
                carries = any(keyword.arg == "rate_limit" for keyword in node.keywords)
                found.append((str(path.relative_to(SRC)), node.lineno, carries))
    return found


def test_the_guard_sees_the_known_catch_sites() -> None:
    # A guard that finds nothing proves nothing.
    assert len(_pending_results_built_in_handlers()) >= 4


def test_every_pending_result_built_from_a_caught_error_carries_its_rate_limit() -> None:
    missing = [
        f"{path}:{line}"
        for path, line, carries in _pending_results_built_in_handlers()
        if not carries
    ]
    assert not missing, (
        "RecoveryAttemptPending built in an except handler without "
        "rate_limit=host_rate_limit_of(error): " + ", ".join(missing)
    )

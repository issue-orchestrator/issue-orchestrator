"""Every recovery or publication result built from a caught error carries its rate limit.

Recovery operations catch their own errors and return a pending result
(#7387) rather than re-raising, so claims, leases and the disposition gate
exit through their tested paths. The price is that the host's typed rate
limit must survive every boundary between the caught error and the action
liveness owner. A boundary that drops it turns a GitHub rate limit into an
ordinary failure that spends retry budget (the class #7303 threaded through
site by site). There are three kinds of boundary, and this guard checks each:

A. A caught error becomes a result: inside ``except ... as error`` every
   result of the family below (or a helper returning one) passes
   ``rate_limit=host_rate_limit_of(error)`` -- the handler's own bound name,
   not ``None`` and not another error.
B. A result becomes another result: a call that takes ``source.message``
   (or ``source.error``) also takes ``source.rate_limit`` (or
   ``source.host_rate_limit``), so a pending result made from a publication
   outcome, or an ``ActionResult`` made from a pending result, keeps it.
C. A result becomes an exception: ``raise E(source.message)`` chains
   ``from rate_limit_cause(source.rate_limit)`` so ``host_rate_limit_of``
   finds it wherever the exception is caught.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "issue_orchestrator"

#: The recovery and publication result family (#7350). Each declares
#: ``rate_limit: HostRateLimit | None`` guarded by
#: ``require_limit_only_on_failure``.
CARRIERS = frozenset({
    "RecoveryAttemptPending",
    "PublicationVerification",
    "BranchWriteOutcome",
    "PrEnsureOutcome",
    "PublishValidatedHeadOutcome",
    "FinalizationOutcome",
    "RecoveryBlockReconcileOutcome",
    "RecoveryBlockReleaseOutcome",
    "PullRequestPreparationRefusal",
})

#: Results that forward a limit under ``host_rate_limit`` instead: checked as
#: the target of a conversion (B), since their own catch sites use
#: ``ActionResult.fail_from``.
CONVERSION_TARGETS = frozenset({"ActionResult"})

#: What a result's limit is called, by the field its message is read from.
LIMIT_FIELDS = ("rate_limit", "host_rate_limit")
MESSAGE_FIELDS = frozenset({"message", "error"})

#: Exceptions that end the caller's authority rather than report a host
#: answer. A handler catching only these may build a result without binding.
AUTHORITY_ONLY = frozenset({"ValidatedWorkClaimLost", "ValidatedWorkAuthorityUnavailable"})

#: Sources whose message is converted but whose type cannot carry a limit,
#: keyed by (module, source expression), with the reason.
NOT_CARRIERS = {
    ("control/retained_completion_preparation.py", "policy_refusal"): (
        "ProcessingResult from completion policy checks (reserved labels, role, "
        "tech-lead shaping, validation): local, never a host read. PR preparation "
        "refusals are typed (PullRequestPreparationRefusal) and carry their limit"
    ),
    ("control/validated_work_scope_retirement.py", "self"): (
        "ScopeRetirement: a store compare-and-set outcome, never a host error"
    ),
    ("control/completion_pr_collision.py", "push_result"): "a local git push result",
    ("control/publication_source_guards.py", "paths"): (
        "BranchPathsResult: a local git path query (the standing-rulings review rule, #8141)"
    ),
    ("control/staged_published_work_finalizer.py", "checkpoint"): (
        "FinalizationCheckpoint: a durable phase the store recorded, not a host answer"
    ),
    ("execution/providers.py", "result"): "a startup token validation, not a retried action",
}


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    location: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.location}: {self.detail}"


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _owner(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


_CARRIER_NAME = re.compile(r"\b(" + "|".join(sorted(CARRIERS)) + r")\b")


def _returns(tree: ast.AST) -> dict[str, set[bool]]:
    found: dict[str, set[bool]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            returns = ast.unparse(node.returns) if node.returns is not None else ""
            found.setdefault(node.name, set()).add(bool(_CARRIER_NAME.search(returns)))
    return found


def builders_in(*trees: ast.AST) -> set[str]:
    """Functions whose declared return names a carrier: they build one.

    Matched by name, so a name that is ALSO defined returning something else
    (``publish`` is both the publisher's stage and ``EventSink.publish``) is
    ambiguous and left out rather than guessed.
    """
    merged: dict[str, set[bool]] = {}
    for tree in trees:
        for name, kinds in _returns(tree).items():
            merged.setdefault(name, set()).update(kinds)
    return {name for name, kinds in merged.items() if kinds == {True}}


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"<bare>"}
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {ast.unparse(kind).rsplit(".", 1)[-1] for kind in types}


def _is_limit_of(value: ast.expr | None, bound: str) -> bool:
    return (
        isinstance(value, ast.Call)
        and _callee(value) == "host_rate_limit_of"
        and len(value.args) == 1
        and not value.keywords
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == bound
    )


def _arguments(call: ast.Call) -> list[ast.expr]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def _message_sources(nodes: list[ast.expr], *, nested: bool) -> set[str]:
    sources: set[str] = set()
    for node in nodes:
        candidates = ast.walk(node) if nested else [node]
        for candidate in candidates:
            if isinstance(candidate, ast.Attribute) and candidate.attr in MESSAGE_FIELDS:
                sources.add(ast.unparse(candidate.value))
    return sources


def _limit_of(value: ast.expr | None, source: str) -> bool:
    return (
        isinstance(value, ast.Attribute)
        and value.attr in LIMIT_FIELDS
        and ast.unparse(value.value) == source
    )


class _Checker(ast.NodeVisitor):
    def __init__(self, module: str, builders: set[str]) -> None:
        self.module = module
        self.builders = builders
        self.findings: list[Finding] = []
        self.sites = {"A": 0, "B": 0, "C": 0}
        self._handlers: list[ast.ExceptHandler] = []

    def _at(self, node: ast.AST) -> str:
        return f"{self.module}:{getattr(node, 'lineno', 0)}"

    def _exempt(self, source: str) -> bool:
        return (self.module, source) in NOT_CARRIERS

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._handlers.append(node)
        for statement in node.body:
            self.visit(statement)
        self._handlers.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee(node)
        builds = name in CARRIERS or name in self.builders
        if builds and self._handlers:
            self._caught_error_boundary(node, self._handlers[-1])
        if builds or _owner(node) in CONVERSION_TARGETS or name in CONVERSION_TARGETS:
            self._conversion_boundary(node)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if isinstance(node.exc, ast.Call):
            for source in _message_sources(_arguments(node.exc), nested=True):
                if self._exempt(source):
                    continue
                self.sites["C"] += 1
                cause = node.cause
                chained = (
                    isinstance(cause, ast.Call)
                    and _callee(cause) == "rate_limit_cause"
                    and len(cause.args) == 1
                    and _limit_of(cause.args[0], source)
                )
                if not chained:
                    self.findings.append(Finding(
                        "C", self._at(node),
                        f"raise built from {source}'s message must chain "
                        f"`from rate_limit_cause({source}.<rate limit>)`",
                    ))
        self.generic_visit(node)

    def _caught_error_boundary(self, call: ast.Call, handler: ast.ExceptHandler) -> None:
        self.sites["A"] += 1
        if handler.name is None:
            if not _caught_names(handler) <= AUTHORITY_ONLY:
                self.findings.append(Finding(
                    "A", self._at(call),
                    f"{_callee(call)} built in a handler that does not bind its error; "
                    "bind it and pass rate_limit=host_rate_limit_of(<name>)",
                ))
            return
        passed = {keyword.arg: keyword.value for keyword in call.keywords}
        if not _is_limit_of(passed.get("rate_limit"), handler.name):
            self.findings.append(Finding(
                "A", self._at(call),
                f"{_callee(call)} built from a caught error must pass "
                f"rate_limit=host_rate_limit_of({handler.name})",
            ))

    def _conversion_boundary(self, call: ast.Call) -> None:
        arguments = _arguments(call)
        for source in _message_sources(arguments, nested=False):
            if self._exempt(source):
                continue
            self.sites["B"] += 1
            if not any(_limit_of(argument, source) for argument in arguments):
                self.findings.append(Finding(
                    "B", self._at(call),
                    f"{_callee(call)} built from {source}.message must also take "
                    f"{source}'s rate limit",
                ))


def check(source: str, *, module: str = "fixture.py", builders: set[str] | None = None) -> _Checker:
    tree = ast.parse(source)
    checker = _Checker(module, builders if builders is not None else builders_in(tree))
    checker.visit(tree)
    return checker


def _check_src() -> _Checker:
    trees = {
        str(path.relative_to(SRC)): ast.parse(path.read_text(), filename=str(path))
        for path in sorted(SRC.rglob("*.py"))
    }
    builders = builders_in(*trees.values())
    total = _Checker("src", builders)
    for module, tree in trees.items():
        # A name ambiguous across src is still unambiguous inside the module
        # that defines it (``_Progress.outcome`` in the finalizer).
        checker = _Checker(module, builders | builders_in(tree))
        checker.visit(tree)
        total.findings.extend(checker.findings)
        for rule, count in checker.sites.items():
            total.sites[rule] += count
    return total


def test_every_recovery_boundary_carries_the_caught_rate_limit() -> None:
    findings = _check_src().findings
    assert not findings, "\n".join(str(finding) for finding in findings)


def test_the_guard_sees_every_boundary_kind() -> None:
    # A guard that finds nothing proves nothing: each rule must see real sites.
    sites = _check_src().sites
    assert sites["A"] >= 20, sites
    assert sites["B"] >= 8, sites
    assert sites["C"] >= 8, sites


def test_every_not_carrier_exemption_names_a_live_site() -> None:
    for module, _source in NOT_CARRIERS:
        assert (SRC / module).is_file(), module


_HANDLER = """
def op():
    try:
        read()
    except RemoteError as error:
        return RecoveryAttemptPending(str(error){limit})
"""


@pytest.mark.parametrize(
    ("limit", "passes"),
    [
        ("", False),
        (", rate_limit=None", False),
        (", rate_limit=host_rate_limit_of(other)", False),
        (", rate_limit=host_rate_limit_of(error.__cause__)", False),
        (", rate_limit=host_rate_limit_of(error)", True),
    ],
    ids=["omitted", "none", "wrong-name", "not-the-name", "bound-name"],
)
def test_a_caught_error_must_forward_its_own_limit(limit: str, passes: bool) -> None:
    findings = check(_HANDLER.format(limit=limit)).findings
    assert (not findings) is passes, findings


def test_a_helper_returning_a_carrier_is_a_boundary_too() -> None:
    source = """
class Executor:
    def _failure(self, message: str) -> PrEnsureOutcome: ...
    def ensure(self):
        try:
            read()
        except RemoteError as exc:
            return self._failure(str(exc))
"""
    assert [finding.rule for finding in check(source).findings] == ["A"]


def test_a_handler_must_bind_the_error_it_builds_from() -> None:
    unbound = """
def op():
    try:
        read()
    except RemoteError:
        return RecoveryAttemptPending("unreadable")
"""
    authority = """
def op():
    try:
        read()
    except ValidatedWorkClaimLost:
        return RecoveryAttemptPending("superseded")
"""
    assert [finding.rule for finding in check(unbound).findings] == ["A"]
    assert check(authority).findings == []


def test_the_innermost_handler_names_the_error() -> None:
    source = """
def op():
    try:
        read()
    except RemoteError as outer:
        try:
            write()
        except WriteError as inner:
            return RecoveryAttemptPending(str(inner), rate_limit=host_rate_limit_of(outer))
"""
    assert [finding.rule for finding in check(source).findings] == ["A"]


@pytest.mark.parametrize(
    ("call", "passes"),
    [
        ("RecoveryAttemptPending(outcome.message, outcome.failure)", False),
        ("RecoveryAttemptPending(outcome.message, rate_limit=other.rate_limit)", False),
        ("RecoveryAttemptPending(outcome.message, rate_limit=outcome.rate_limit)", True),
        ("ActionResult.fail(action, result.message)", False),
        ("ActionResult.fail_limited(action, result.message, result.rate_limit)", True),
    ],
    ids=["pending-omitted", "pending-other", "pending-forwarded", "action-fail", "action-limited"],
)
def test_a_converted_result_keeps_its_source_limit(call: str, passes: bool) -> None:
    findings = check(f"def op():\n    return {call}\n").findings
    assert (not findings) is passes, findings


@pytest.mark.parametrize(
    ("statement", "passes"),
    [
        ("raise RuntimeError(result.error)", False),
        ("raise RuntimeError(f'failed: {result.error}') from None", False),
        ("raise RuntimeError(result.error) from rate_limit_cause(other.host_rate_limit)", False),
        ("raise RuntimeError(result.error) from rate_limit_cause(result.host_rate_limit)", True),
        ("raise Deferred(self.message) from rate_limit_cause(self.rate_limit)", True),
    ],
    ids=["unchained", "from-none", "other-source", "chained", "own-limit"],
)
def test_a_result_raised_as_an_exception_chains_its_limit(statement: str, passes: bool) -> None:
    findings = check(f"def op():\n    {statement}\n").findings
    assert (not findings) is passes, findings


def test_a_successful_result_cannot_carry_a_rate_limit() -> None:
    """The family's shared invariant: a limit explains a failure, nothing else."""
    from datetime import datetime, timezone

    from issue_orchestrator.domain.exact_git import ExactPushOutcome
    from issue_orchestrator.domain.host_rate_limit import HostRateLimit
    from issue_orchestrator.domain.validated_head_publication import (
        BranchWriteOutcome,
        BranchWriteStatus,
    )

    limit = HostRateLimit(datetime(2026, 9, 27, 15, 5, tzinfo=timezone.utc), "primary")
    with pytest.raises(ValueError, match="only a retryable failed branch stage"):
        BranchWriteOutcome(
            BranchWriteStatus.PUSHED, "a" * 40, ExactPushOutcome.PUSHED, None, "pushed",
            rate_limit=limit,
        )

"""No new direct session-kind comparison outside the kind's owner (#7347).

Before #7347 about forty policy sites each decided for themselves which kinds of
session did what, by comparing ``TaskKind`` members, parsing terminal-name
prefixes or matching agent labels, and they disagreed. The answers now live in
ONE capability table (``domain/session_kind.py``: ``kind.capabilities``), and
policy asks the table.

This is a STATIC guard: it parses every module under ``src/issue_orchestrator``
without importing anything and fails on any direct comparison against a
``SessionKind`` member (``kind is SessionKind.X``, ``kind in {SessionKind.X,
...}``, ``SessionKind.X.value == ...``), any collection literal of members (a
private policy table), or a ``match`` on a member - outside the owner module and
outside the per-kind handlers registered below. A registered handler is behaviour
genuinely specific to one kind - verdict routing (G), workspace shape (H),
tech-lead exclusivity and identity (I), presentation (J) - which the capability
table deliberately does not model.

The registry must match the tree EXACTLY: a new site fails, and so does a
registered site that no longer compares (a stale entry would silently license
the next one). To add a site, first ask whether a capability answers the
question; register it only if the behaviour belongs to one kind alone.

Limits: it sees member attributes spelled ``SessionKind.X``. A kind reached by
another alias, or compared as a bare string, is not seen.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "issue_orchestrator"
_OWNER = "domain/session_kind.py"

# (module, enclosing function) -> number of direct kind comparisons it may make.
_PER_KIND_HANDLERS: dict[tuple[str, str], int] = {
    # G - what a verdict drives: the review machine for a review, the rework
    # machine for a rework; a retrospective review's own follow-ups; a rework's
    # PR trigger restored after a provider block.
    ("control/completion_handler.py", "CompletionHandler._update_state_machines"): 2,
    ("control/completion_pr_lookup.py", "CompletionPrLookup.for_session"): 1,
    ("control/retrospective_review_completion.py", "retrospective_review_completion_actions"): 1,
    ("control/session_completion.py", "_queue_rework_after_retrospective_changes"): 1,
    ("control/provider_blocked_completion.py", "provider_blocked_actions"): 1,
    ("control/session_restorer.py", "SessionRestorer._restore_single_session"): 2,
    # H - a tech-lead run's workspace: preserve its anchor branch, discard its
    # launch-authority row on a failed launch, prepare its per-flavor inputs.
    ("control/session_launcher.py", "SessionLauncher.launch_issue_session"): 1,
    ("control/session_launcher.py", "SessionLauncher._discard_tech_lead_authority_after_failed_launch"): 1,
    ("control/tech_lead_session_policy.py", "prepare_tech_lead_session_data"): 1,
    ("control/tech_lead_session_policy.py", "recover_tech_lead_launch_scope"): 1,
    # I - tech-lead exclusivity, capacity and identity: the global barrier and
    # reserved budget count tech-lead runs; artifact holds and reaction
    # suppression name them; the launch-authority row belongs to them alone.
    ("control/tech_lead_run_admission.py", "active_tech_lead_sessions"): 1,
    ("control/worker_budget.py", "active_tech_lead_session_count"): 1,
    ("control/tech_lead_artifact_retention.py", "tech_lead_problem_artifact_hold_issue_numbers"): 1,
    ("control/tech_lead_reaction.py", "record_completed_session_problem"): 1,
    ("control/health_review_trigger.py", "recover_pending_tech_lead_anchors"): 1,
    ("domain/models.py", "PendingValidationRetry.__post_init__"): 1,
    ("domain/registered_completion.py", "CompletionRunRole.__post_init__"): 1,
    ("domain/registered_completion.py", "CompletionRunRole.require_processing_role"): 1,
    ("domain/registered_completion.py", "CompletionProcessingPolicy.is_tech_lead"): 1,
    # J - presentation and identity: dashboard phase words, and "is this the
    # retrospective review of that issue".
    ("view_models/dashboard.py", "_build_active_items"): 5,
    ("domain/models.py", "is_retrospective_review_session"): 1,
}


def _is_member(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "SessionKind"
        and node.attr.isupper()
    )


class _KindComparisons(ast.NodeVisitor):
    def __init__(self) -> None:
        self._scope: list[str] = []
        self.found: collections.Counter[str] = collections.Counter()

    def _count(self) -> None:
        self.found[".".join(self._scope) or "<module>"] += 1

    def _enter(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def visit_Compare(self, node: ast.Compare) -> None:
        # One comparison is one site: a kind set compared against is counted
        # with the comparison, not again as a collection.
        if any(_is_member(sub) for part in (node.left, *node.comparators) for sub in ast.walk(part)):
            self._count()
            return
        self.generic_visit(node)

    def _collection(self, node: ast.AST, elements: list[ast.expr]) -> None:
        if any(_is_member(element) for element in elements):
            self._count()
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> None:
        self._collection(node, node.elts)

    def visit_List(self, node: ast.List) -> None:
        self._collection(node, node.elts)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._collection(node, node.elts)

    def visit_Dict(self, node: ast.Dict) -> None:
        self._collection(node, [key for key in node.keys if key is not None])

    def visit_MatchValue(self, node: ast.MatchValue) -> None:
        if _is_member(node.value):
            self._count()
        self.generic_visit(node)


def _direct_kind_comparisons(source: str) -> collections.Counter[str]:
    visitor = _KindComparisons()
    visitor.visit(ast.parse(source))
    return visitor.found


def test_no_direct_kind_comparison_outside_the_owner_and_its_registered_handlers() -> None:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel == _OWNER:
            continue
        for scope, count in _direct_kind_comparisons(path.read_text(encoding="utf-8")).items():
            found[(rel, scope)] = count

    unregistered = {site: n for site, n in found.items() if site not in _PER_KIND_HANDLERS}
    assert not unregistered, (
        "direct SessionKind comparisons outside domain/session_kind.py; ask "
        f"kind.capabilities instead, or register a per-kind handler: {unregistered}"
    )
    changed = {
        site: (_PER_KIND_HANDLERS[site], found.get(site, 0))
        for site in _PER_KIND_HANDLERS
        if found.get(site, 0) != _PER_KIND_HANDLERS[site]
    }
    assert not changed, f"registry drift (registered, found): {changed}"


def test_the_guard_sees_every_shape_of_direct_comparison() -> None:
    """Pin what the scanner catches, so a refactor of it cannot go blind."""
    source = '''
def a(kind):
    return kind is SessionKind.CODE
def b(kind):
    return kind in {SessionKind.CODE, SessionKind.REWORK}
def c(value):
    return value == SessionKind.REVIEW.value
_TABLE = (SessionKind.TECH_LEAD, SessionKind.CODE)
def d(kind):
    match kind:
        case SessionKind.REWORK:
            return 1
def e(kind):
    return kind.capabilities.capturable
def f(issue):
    return SessionKind.CODE.terminal_name(issue)
'''
    assert _direct_kind_comparisons(source) == collections.Counter(
        {"a": 1, "b": 1, "c": 1, "<module>": 1, "d": 1}
    )

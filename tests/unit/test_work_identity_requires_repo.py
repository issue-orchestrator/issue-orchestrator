"""Work identity must carry a repository scope, and must say so early.

Regression suite for #7255. A tech-lead session was launched from an `Issue`
built without a repo, so `Issue.key.scope()` was `""` and the run ledger stored
`issue_scope=''`. Nothing on the way in objected. The only validator lives at
teardown, in `ValidatedWorkKey`, and by then the damage is unrecoverable: the
`ValueError` landed inside `handle_session_completion` after the terminal
transition had been logged and before any of its effects ran, so the session was
never dropped, never killed and never labelled -- and the next tick "completed"
it again. 218 times over two hours, with the operator's Terminate button
returning 500 on the identical exception.

These tests pin the value down at each boundary it crosses.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import MagicMock
from pathlib import Path

import pytest

from issue_orchestrator.domain.issue_run_evidence import (
    IssueRunEvidenceUnavailable,
    IssueRunRecord,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.execution.issue_run_codec import IssueRunRow
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.repo_scope import require_repo

from .session_run_helpers import make_session_run_assets


@dataclass(frozen=True)
class _UnscopedIssueKey:
    """An IssueKey whose scope is empty without raising, to reach the codec guard."""

    def stable_id(self) -> str:
        return "7255"

    def scope(self) -> str:
        return ""

    def __str__(self) -> str:
        return "unscoped:7255"


class TestIssueIdentity:
    def test_key_carries_the_repo_scope(self) -> None:
        issue = Issue(number=7255, title="Health Review", labels=[], repo="acme/widgets")

        assert issue.key.scope() == "acme/widgets"

    def test_scope_refuses_a_key_with_no_repo(self) -> None:
        """Fail on the half that needs a repo, at the call that poisoned the ledger."""
        issue = Issue(number=7255, title="Health Review", labels=[])

        with pytest.raises(ValueError, match="no repository scope"):
            issue.key.scope()

    def test_stable_id_still_works_without_a_repo(self) -> None:
        """`stable_id()` needs no repo, and many callers want only that.

        Refusing in `Issue.key` instead would punish them: `awaiting_merge_
        reconciler.py:580` reads `issue.key.stable_id()` and never touches scope.
        """
        issue = Issue(number=7255, title="[M1-011] Health Review", labels=[])

        assert issue.key.stable_id() == "M1-011"

    def test_refusal_names_the_issue(self) -> None:
        """The old failure named `repo_slug` and nothing else -- no issue, no session."""
        with pytest.raises(ValueError, match="7255"):
            Issue(number=7255, title="Health Review", labels=[]).key.scope()


class TestRequireRepo:
    def test_returns_the_configured_slug(self) -> None:
        assert require_repo(Config(repo="acme/widgets")) == "acme/widgets"

    @pytest.mark.parametrize("value", [None, ""])
    def test_raises_instead_of_degrading_to_empty(self, value) -> None:
        """This replaces `config.repo or ""`, which is how "" got in circulation."""
        with pytest.raises(ValueError, match="config.repo is required"):
            require_repo(Config(repo=value))


class TestRunLedgerWriteBoundary:
    """`issue_scope TEXT NOT NULL` is satisfied by "", so the column never caught it."""

    def _record(self, tmp_path: Path, scope: str) -> IssueRunRecord:
        return IssueRunRecord(
            session_key=SessionKey(
                issue=GitHubIssueKey(repo=scope, external_id="7255"), task=TaskKind.CODE
            ),
            run=make_session_run_assets(tmp_path, session_name="issue-7255"),
            recorded_at="2026-09-11T21:04:01+00:00",
            branch_name="tech-lead-investigation-7255",
            terminal_binding=None,
        )

    def test_persists_a_scoped_run(self, tmp_path: Path) -> None:
        row = IssueRunRow.from_record(7255, self._record(tmp_path, "acme/widgets"))

        assert row.data["issue_scope"] == "acme/widgets"

    def test_refuses_to_persist_an_unscoped_run(self, tmp_path: Path) -> None:
        """A poisoned row outlives the launch; refusing costs one launch.

        `GitHubIssueKey.scope()` refuses first, so this guard is reached only by
        an IssueKey implementation that returns "" -- which is exactly why it is
        implementation-agnostic rather than a repeat of the same check.
        """
        record = self._record(tmp_path, "acme/widgets")
        unscoped = SessionKey(issue=_UnscopedIssueKey(), task=TaskKind.CODE)
        record = replace(record, session_key=unscoped)

        # IssueRunEvidenceUnavailable, not ValueError: `record_run` builds the
        # codec outside its try and `WorktreeContext.create` catches only this
        # type, so a bare ValueError would escape unhandled AFTER the worktree
        # and branch were created.
        with pytest.raises(IssueRunEvidenceUnavailable, match="empty issue scope"):
            IssueRunRow.from_record(7255, record)

    def test_a_github_key_with_no_repo_refuses_before_the_codec(self, tmp_path: Path) -> None:
        """`scope()` raises first, but the codec still answers in ITS type.

        `record_run` builds this codec outside its try and `WorktreeContext.create`
        catches only `IssueRunEvidenceUnavailable`, so letting the raw ValueError
        through would escape unhandled after the worktree and branch exist.
        """
        with pytest.raises(IssueRunEvidenceUnavailable, match="no repository scope"):
            IssueRunRow.from_record(7255, self._record(tmp_path, ""))

    def test_an_already_poisoned_row_is_refused_on_READ(self, tmp_path: Path) -> None:
        """Rows written before the write guard existed must not enter the domain.

        Three such rows exist in the live state dir from the #7255 incident. An
        unscoped row decodes into a `GitHubIssueKey` whose `scope()` now raises,
        so without this guard it would raise from wherever the record travelled
        -- `SessionKey.__hash__`, `SessionKey.__eq__`,
        `manual_completion_preparation` -- rather than at the storage boundary.
        `recorded_runs` maps this to the typed `IssueRunEvidenceUnavailable`.
        """
        good = IssueRunRow.from_record(7255, self._record(tmp_path, "acme/widgets"))
        poisoned = IssueRunRow({**good.data, "issue_scope": ""})

        with pytest.raises(IssueRunEvidenceUnavailable, match="no repository scope"):
            poisoned.decode()

    def test_a_scoped_row_still_decodes(self, tmp_path: Path) -> None:
        row = IssueRunRow.from_record(7255, self._record(tmp_path, "acme/widgets"))

        assert row.decode().session_key.issue.scope() == "acme/widgets"


class TestStoredKeysAreRefusedOnTheWayOut:
    """Every decode boundary refuses an unscoped stored key, not just the ledger.

    `domain/attempt.py` already did this for the attempt sidecar. The run ledger
    and the pending-work claim store did not, so a row written during the #7255
    window would decode into a `GitHubIssueKey` whose `scope()` now raises --
    surfacing far from the bad row.
    """

    def test_pending_work_claim_with_no_scope_is_refused(self) -> None:
        from issue_orchestrator.execution.pending_work_codec import (
            PendingWorkClaimDecodeError,
            _decode_issue_key,
        )

        with pytest.raises(PendingWorkClaimDecodeError, match="no repository scope"):
            _decode_issue_key({"scope": "", "stable_id": "7255"})

    def test_a_scoped_pending_work_claim_still_decodes(self) -> None:
        from issue_orchestrator.execution.pending_work_codec import _decode_issue_key

        key = _decode_issue_key({"scope": "acme/widgets", "stable_id": "7255"})

        assert key.scope() == "acme/widgets"


class TestTheLedgerRepairsRowsItCannotOtherwiseRead:
    """A poisoned row must never take down reads for every OTHER issue.

    `IssueRunEvidenceService.issues_for_worktree` sweeps every issue number in
    the ledger, and `recorded_runs` raises `IssueRunEvidenceUnavailable` for a
    whole issue. So refusing a poisoned row outright would make `preserve_cleanup`
    and `preserve_worktree` raise for EVERY issue -- worktrees never removed,
    cleanup re-planned every tick, forever, on an append-only table with no
    prune. That is the same shape as the bug being fixed, so the ledger repairs
    the row instead.
    """

    def _ledger(self, tmp_path: Path, repo_slug: str):
        from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger

        return SqliteIssueRunLedger(tmp_path / "issue_run_ledger.sqlite", repo_slug=repo_slug)

    def _poison(self, tmp_path: Path) -> None:
        import sqlite3

        with sqlite3.connect(tmp_path / "issue_run_ledger.sqlite") as conn:
            conn.execute("UPDATE issue_runs SET issue_scope=''")

    def _record_one(self, tmp_path: Path, ledger) -> None:
        ledger.record_run(
            7255,
            IssueRunRecord(
                session_key=SessionKey(
                    issue=GitHubIssueKey(repo="acme/widgets", external_id="7255"),
                    task=TaskKind.CODE,
                ),
                run=make_session_run_assets(tmp_path, session_name="issue-7255"),
                recorded_at="2026-09-11T21:04:01+00:00",
                branch_name="tech-lead-investigation-7255",
                terminal_binding=None,
            ),
        )

    def test_reopening_backfills_an_unscoped_row(self, tmp_path: Path) -> None:
        ledger = self._ledger(tmp_path, "acme/widgets")
        self._record_one(tmp_path, ledger)
        self._poison(tmp_path)

        reopened = self._ledger(tmp_path, "acme/widgets")

        runs = reopened.recorded_runs(7255)
        assert len(runs) == 1
        assert runs[0].session_key.issue.scope() == "acme/widgets"

    @pytest.mark.parametrize(
        "bad_scope", ["", "   ", "\t", "\n", "\x0b", "\x1f", "\xa0", "\u2003"]
    )
    def test_reopening_repairs_every_blank_form(self, tmp_path: Path, bad_scope: str) -> None:
        """The backfill and the readability rule must agree on "blank".

        They did not, twice: first the backfill matched `issue_scope=''`
        exactly, then it spelled out an ASCII whitespace set -- both narrower
        than `.strip()`, so some whitespace scopes were never repaired AND
        always skipped. The repair now applies the Python predicate itself, so
        the two agree by construction rather than by enumeration. `recorded_runs` then returned `()`, which
        `evidence_for_issue` reports as NO_RUNS_RECORDED and capture reads as
        "nothing to preserve" -- a false all-clear over unreadable work.
        """
        import sqlite3

        ledger = self._ledger(tmp_path, "acme/widgets")
        self._record_one(tmp_path, ledger)
        with sqlite3.connect(tmp_path / "issue_run_ledger.sqlite") as conn:
            conn.execute("UPDATE issue_runs SET issue_scope=?", (bad_scope,))

        reopened = self._ledger(tmp_path, "acme/widgets")

        runs = reopened.recorded_runs(7255)
        assert len(runs) == 1
        assert runs[0].session_key.issue.scope() == "acme/widgets"

    def test_a_row_inserted_after_the_backfill_is_repaired_on_read(
        self, tmp_path: Path
    ) -> None:
        """Another instance on an older build can insert one after open.

        Raising for it would fail `recorded_runs` for this issue ->
        `evidence_for_issue` -> `issues_for_worktree`, which sweeps EVERY issue:
        worktree custody cleanup stops repo-wide and re-plans every tick. That
        is #7255's own shape, reintroduced by its fix.
        """
        import sqlite3

        ledger = self._ledger(tmp_path, "acme/widgets")
        self._record_one(tmp_path, ledger)
        # Already open: the open-time backfill cannot see this.
        with sqlite3.connect(tmp_path / "issue_run_ledger.sqlite") as conn:
            conn.execute("UPDATE issue_runs SET issue_scope=''")

        runs = ledger.recorded_runs(7255)

        assert len(runs) == 1
        assert runs[0].session_key.issue.scope() == "acme/widgets"

    def test_an_unrepairable_row_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """With no slug to repair with, the read degrades instead of failing.

        Loudly: the skip logs at ERROR. What it must NOT do is raise, because
        that raise propagates to every issue in the ledger.
        """
        ledger = self._ledger(tmp_path, "acme/widgets")
        self._record_one(tmp_path, ledger)
        self._poison(tmp_path)

        reopened = self._ledger(tmp_path, "")

        assert reopened.recorded_runs(7255) == ()


def _is_proven_repo(node: ast.expr, *, adapter: bool = False) -> bool:
    """Whether this `repo=` argument cannot be empty or None.

    `repo=config.repo` is `Optional[str]`, so it satisfies a bare "has a repo="
    check while reintroducing the defect in a form only `scope()` catches, much
    later. Accepted as proof: a non-empty literal, a `require_repo(...)` call, a
    typed `repo_slug` field (validated by its own dataclass) and an adapter's
    own `self.repo`, which cannot exist unset. A bare `config.repo`, another
    issue's `.repo` (the domain default is still `""`) and a subscripted read
    are NOT proof -- the guardrail is the mechanism that closes the class, so
    its weakest accepted form is the real bound.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(node.value)
    if isinstance(node, ast.Call):
        func = node.func
        called = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        return called == "require_repo"
    if isinstance(node, ast.Attribute):
        # A typed `repo_slug` field is validated by its own dataclass
        # (`HistoricalIntakeCommand.__post_init__` -> `require_text`), and
        # `self.repo` on an adapter that refuses to exist without one is proven
        # at construction. `config.repo` is `Optional[str]`, and any OTHER
        # object's `.repo` inherits the domain default of `""`.
        if node.attr == "repo_slug":
            return True
        return node.attr == "repo" and adapter and _is_self(node.value)
    return False


def _is_self(node: ast.expr) -> bool:
    """`self.repo` only: an adapter that cannot be constructed without a repo."""
    return isinstance(node, ast.Name) and node.id == "self"


def _adapter_file(rel_path: str) -> bool:
    """`self.repo` is proof only where the class refuses to exist without one.

    `GitHubAdapter` resolves its repo at construction and raises otherwise. A
    domain dataclass with an optional `repo` field would get the same free pass
    from a bare `self.repo` rule, and `Issue.repo` still defaults to `""`.
    """
    return rel_path.startswith("adapters/")


def _issue_constructions_without_repo() -> list[str]:
    """Every `Issue(...)` in src/ that does not pass a repo.

    The domain `Issue` still defaults `repo` to "" -- 670 test constructions make
    removing that default its own change -- so this guardrail holds the line for
    production code instead.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
    # Sites whose repo is proven by a local guard the AST cannot see. Keep this
    # list short and justified; it is the only escape hatch.
    # Keyed by enclosing function, not line number: a line-numbered entry fails
    # closed on any shift and invites a blind renumber instead of re-checking
    # that the local guard still holds.
    allowed = {
        # `_validation_issue_key` builds `repo` from two optionals and returns
        # None unless `if repo and repo.strip()` holds, two lines above.
        ("control/session_completion.py", "_validation_issue_key"),
        # `decode` refuses a blank `issue_scope` immediately above this line, so
        # the row read here is proven non-blank at its storage boundary.
        ("execution/issue_run_codec.py", "decode"),
        # `load_issues` refuses a blank `repo` at its boundary before building
        # any issue, so the parameter is proven for every construction below it.
        ("execution/queue_cache_store.py", "load_issues"),
        # `_resolve_external_id` returns early unless `self.repo` is non-blank,
        # two lines above the construction.
        ("control/dependency_evaluator.py", "_resolve_external_id"),
        # `_issue_key_from_dict` is only reached from `Attempt.from_dict`, which
        # refuses a blank `issue_scope` before rebuilding.
        ("domain/attempt.py", "_issue_key_from_dict"),
        # `_decode_issue_key` refuses a blank stored scope immediately above the
        # construction.
        ("execution/pending_work_codec.py", "_decode_issue_key"),
        # `Issue.key` passes the domain default through deliberately;
        # `GitHubIssueKey.scope()` is its validator, and `stable_id()` has
        # legitimate repo-less callers.
        ("domain/models.py", "key"),
    }
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        enclosing: dict[int, str] = {}
        for scope in ast.walk(tree):
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(scope):
                    if isinstance(inner, ast.Call):
                        enclosing.setdefault(inner.lineno, scope.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name not in {"Issue", "GitHubIssue", "GitHubIssueKey"}:
                continue
            rel = str(path.relative_to(src))
            repo = next((kw.value for kw in node.keywords if kw.arg == "repo"), None)
            if repo is not None and _is_proven_repo(repo, adapter=_adapter_file(rel)):
                continue
            if (rel, enclosing.get(node.lineno, "")) in allowed:
                continue
            offenders.append(f"{rel}:{node.lineno}")
    return offenders


class TestTheAllowlistPremisesAreReal:
    """Each allowlisted site is allowed because a guard proves its repo.

    The allowlist is keyed by function name, so deleting a guard would leave the
    guardrail passing while the class reopens. These pin the guards themselves.
    """

    def test_queue_cache_refuses_to_rebuild_issues_without_a_repo(self, tmp_path) -> None:
        from issue_orchestrator.execution.queue_cache_store import QueueCacheStore

        store = QueueCacheStore(tmp_path / "queue_cache.sqlite")

        with pytest.raises(ValueError, match="without a repository"):
            store.load_issues("   ")

    def test_validation_issue_key_is_none_without_a_repo(self) -> None:
        from issue_orchestrator.control.session_completion import _validation_issue_key

        session = SimpleNamespace(
            issue=SimpleNamespace(repo="   ", number=7255), key=None
        )

        assert _validation_issue_key(session, Config(repo=None)) is None

    def test_attempt_sidecar_refuses_a_blank_scope(self) -> None:
        """`_rebuild_issue_key`'s allowlist entry rests on this guard."""
        from issue_orchestrator.domain.attempt import Attempt

        with pytest.raises(ValueError, match="issue_scope"):
            Attempt.from_dict({
                "schema_version": 1,
                "issue_key_type": "github",
                "issue_key": "7255",
                "issue_scope": "   ",
                "head_sha": "a" * 40,
            })

    def test_external_id_resolution_stops_without_a_repo(self) -> None:
        """`_resolve_external_id`'s allowlist entry rests on this guard.

        Driven through the public evaluator rather than the private method: a
        blank repo must yield an UNKNOWN dependency with an error, never a key.
        """
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
        from issue_orchestrator.domain.dependencies import DependencyState

        evaluator = DependencyEvaluator(
            issue_checker=MagicMock(),
            events=MagicMock(),
            issue_resolver=MagicMock(),
            repo="   ",
        )

        # A matching milestone, so the evaluation reaches external-ID resolution
        # rather than short-circuiting on the milestone gate.
        report = evaluator.evaluate(7255, "Depends-on: M1-011", source_milestone="M1")

        assert report.all_dependencies
        assert report.all_dependencies[0].state is DependencyState.UNKNOWN
        assert report.all_dependencies[0].error


def test_no_production_issue_is_built_without_a_repo() -> None:
    """Close the class, not just the #7255 instance.

    `session_routing.py` was one of six such sites. Any new one reintroduces a
    session that cannot terminalize, so this fails at review time instead. The
    rule covers `Issue`, `GitHubIssue` (the concrete production class) and
    `GitHubIssueKey`: all three mint the identity whose `scope()` feeds
    `issue_runs.issue_scope`.
    """
    assert _issue_constructions_without_repo() == []

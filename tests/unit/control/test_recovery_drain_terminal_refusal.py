"""#7346: a zero-commit validated head reaches a terminal state through the drain.

The live wedge (porchpin#410) chained three defects: the first publish attempt
pushed from the publication checkout, whose pre-push hook left ignored build
output behind; every retry then refused that checkout at ``prepare()``; and the
PR-create 422 that would have ended the record was classified transient. This
drives the same shape through the real drain, record operation, SQLite store,
escrow, Git checkout and push hook, with a PR boundary that answers the way
GitHub does for a head with no commits beyond its base.
"""

import os
import stat
from types import SimpleNamespace

import pytest

from tests.unit.control.liveness_doubles import drain_liveness
from issue_orchestrator.control.recovery_drain import RecoveryDrain
from issue_orchestrator.control.validated_work_scope_retirement import (
    OutOfScopeRecordRetirement,
    OutOfScopeRetirementSweep,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.publication_remote import (
    PrCreateRejection,
    PublicationPrCreateRejected,
    PublicationRemoteError,
)
from issue_orchestrator.domain.recovery_attempt import RecoveryAttemptPending
from issue_orchestrator.domain.recovery_drain import RecoveryDrainMode
from issue_orchestrator.domain.validated_work import (
    PublishValidatedHeadStatus,
    ValidatedWorkFailure,
    ValidatedWorkState,
)
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from issue_orchestrator.ports.recovery_block import NullRecoveryBlockSweep
from issue_orchestrator.ports.retained_claim_maintenance import NullRetainedClaimMaintenance
from tests.unit.control.test_recovery_publication_attempt import publication as publication
from tests.unit.control.test_recovery_publication_completion import completion as completion
from tests.unit.control.test_recovery_record_operation import build_operation
from tests.unit.test_validated_work_preservation import custody as custody

HOOK_OUTPUTS = (
    ".build/index.js",
    "packages/core/.build/lib.js",
    "tsconfig.build.tsbuildinfo",
    ".issue-orchestrator/test-results/junit.xml",
)


@pytest.fixture
def retained(custody):
    """Validated head == main: the branch carries zero commits (porchpin#410)."""
    (custody.repo / ".gitignore").write_text(
        ".issue-orchestrator/\n.build/\n**/.build/\n*.tsbuildinfo\n"
    )
    custody.git.run(custody.repo, ["add", ".gitignore"])
    custody.git.run(custody.repo, ["commit", "-m", "Ignore generated output"])
    # The custody branch carries a commit of its own since #7347; this shape
    # is the branch sitting exactly on main.
    custody.git.run(custody.worktree, ["reset", "--hard", "main"])
    base = custody.git.head_sha(custody.repo)
    assert custody.git.head_sha(custody.worktree) == base
    custody.git.run(custody.repo, ["update-ref", "refs/remotes/origin/main", base])
    hook = custody.repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    writes = "\n".join(
        f'mkdir -p "$top/{os.path.dirname(path)}" && echo generated > "$top/{path}"'
        for path in HOOK_OUTPUTS
    )
    hook.write_text(f"#!/bin/sh\ncat >/dev/null\ntop=$(git rev-parse --show-toplevel)\n{writes}\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    return custody


def _answer_like_github(rig):
    """Lose the first create response; then refuse a head with no new commits."""
    creates = []

    def create_pr(command):
        creates.append(command.branch_name)
        if len(creates) == 1:
            raise PublicationRemoteError("GitHub POST /pulls failed: 502")
        remote = rig.custody.remote
        head = rig.remote.read_branch(command)
        main = rig.custody.git.run(remote, ["rev-parse", "refs/heads/main"]).stdout.strip()
        assert head == main, "the fixture must reproduce the zero-commit shape"
        raise PublicationPrCreateRejected(
            PrCreateRejection.NO_COMMITS,
            f"PR create refused: No commits between main and {command.branch_name}",
        )

    rig.remote.create_pr = create_pr
    return creates


def test_zero_commit_head_reaches_terminal_needs_human_instead_of_looping(completion):
    op = build_operation(completion)
    rig = op.rig
    creates = _answer_like_github(rig)
    checkout = rig.prepared.workspace.checkout

    # First attempt: the push runs the repository hook inside the publication
    # checkout, then the create response is lost -- a genuinely transient failure.
    first = op.owner.run(op.request, OrchestratorState())
    assert isinstance(first, RecoveryAttemptPending)
    assert first.failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert rig.store.get(op.request.record_id).state is ValidatedWorkState.PUBLISHING
    assert all((checkout / path).exists() for path in HOOK_OUTPUTS)

    scope = OutOfScopeRecordRetirement(
        intake=rig.custody.ledger,
        store=rig.store,
        effects=rig.effects,
        blocks=completion.aggregate,
        events=InMemoryEventSink(),
    )
    # Which rule wins: a tech-lead record is retired as outside recovery scope
    # before any publication (tests/unit/test_validated_work_scope.py). This
    # record is a coding run's, so the scope rule keeps it and the 422 path
    # must end it.
    assert scope.recovery_owns_record(rig.store.record_for_id(op.request.record_id))
    now = SimpleNamespace(value=0.0)
    liveness = drain_liveness()
    drain = RecoveryDrain(
        queue=rig.store,
        operation=op.owner,
        authority_refresh=None,
        claim_maintenance=NullRetainedClaimMaintenance(),
        block_sweep=NullRecoveryBlockSweep(),
        # Wired as bootstrap wires it (#7323): recovery's scope rule runs
        # first. This record comes from a CODING run, so the rule keeps it
        # and the publication path below is what ends it.
        scope_sweep=OutOfScopeRetirementSweep(
            source=rig.store,
            store=rig.store,
            execution=rig.execution,
            retirement=scope,
            batch_size=5,
            liveness=liveness,
        ),
        batch_size=5,
        interval_seconds=10,
        liveness=liveness,
        clock=lambda: now.value,
    )

    def active():
        return RecoveryDrainMode.ACTIVE

    report = drain.tick(OrchestratorState(), active)
    assert report.scope_sweep.retired == ()
    assert [item.record_id for item in report.items] == [op.request.record_id]
    outcome = report.items[0].outcome
    assert isinstance(outcome, RecoveryAttemptPending)
    assert outcome.failure is ValidatedWorkFailure.PR_CREATE_NO_COMMITS, outcome.message

    record = rig.store.record_for_id(op.request.record_id)
    assert record.disposition.state is ValidatedWorkState.FAILED
    assert record.disposition.failure is ValidatedWorkFailure.PR_CREATE_NO_COMMITS
    attempts = rig.store.publish_attempts(op.request.record_id)
    assert [attempt.outcome for attempt in attempts] == [
        PublishValidatedHeadStatus.TRANSIENT_FAILURE,
        PublishValidatedHeadStatus.REJECTED,
    ]
    assert creates == ["feature", "feature"]
    # Escalated, never discarded: the validated commit stays pinned in escrow.
    assert rig.custody.escrow.verifies(record.current_evidence)
    assert rig.store.owner_of(op.request.record_id) is None

    for tick in range(1, 4):
        now.value = 10.0 * tick
        later = drain.tick(OrchestratorState(), active)
        assert later.items == ()
        # A coding run's FAILED record is a human's decision, never retired
        # as out of recovery scope.
        assert later.scope_sweep.retired == ()
    assert rig.store.get(op.request.record_id).state is ValidatedWorkState.FAILED
    assert creates == ["feature", "feature"]

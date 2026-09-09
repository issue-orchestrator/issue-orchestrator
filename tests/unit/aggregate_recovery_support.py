"""Real aggregate/finalizer/store composition with an interruptible remote port."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
import sys

from issue_orchestrator.adapters.issue_disposition_gate import (
    FileIssueDispositionMutationGate,
)
from issue_orchestrator.control.actions import ActionResult, AddLabelAction
from issue_orchestrator.control.aggregate_recovery_block import AggregateRecoveryBlocks
from issue_orchestrator.control.operator_validated_work_abandonment import (
    OperatorValidatedWorkAbandonment,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import NeedsHumanBlock
from issue_orchestrator.control.retry_review_routing import RetryReviewPolicy
from issue_orchestrator.control.validated_work_admission import RankedEvidenceAdmission
from issue_orchestrator.ports.completion_intake import CompletionIntakeLedger
from issue_orchestrator.control.staged_published_work_finalizer import (
    StagedPublishedWorkFinalizer,
)
from issue_orchestrator.execution.pending_work_claim_store import (
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from tests.unit.staged_finalization_support import Crash, FinalizationRig
from tests.unit.validated_work_support import AT
from tests.process_group_run import run_in_process_group


@dataclass
class RecoveryRemote:
    labels: set[str] = field(
        default_factory=lambda: {"recovery-pending", "blocked-failed"}
    )
    actions: list[tuple[str, str]] = field(default_factory=list)
    crash_after: str = ""
    fail_remove: str = ""
    fail_add: str = ""
    after_read: Callable[[], None] = lambda: None
    before_remove: Callable[[], None] = lambda: None

    def read_issue_labels(self, issue_number):
        assert issue_number == 6914
        labels = sorted(self.labels)
        self.after_read()
        return labels

    def add_label(self, issue_number, label):
        assert issue_number == 6914
        if label == self.fail_add:
            raise OSError("lost remote write")
        self.labels.add(label)
        self.actions.append(("add", label))

    def remove_label(self, issue_number, label):
        assert issue_number == 6914
        self.before_remove()
        if label == self.fail_remove:
            raise OSError("lost remote write")
        self.labels.discard(label)
        self.actions.append(("remove", label))
        if label == self.crash_after:
            raise Crash()

    def apply(self, action):
        if isinstance(action, AddLabelAction):
            self.add_label(action.issue_number, action.label)
        else:
            self.remove_label(action.issue_number, action.label)
        return ActionResult.ok(action)


class AggregateRig:
    def __init__(self, path: Path):
        self.path = path
        self.base = FinalizationRig(path / "work.sqlite")
        self.remote = RecoveryRemote()
        self.gate = FileIssueDispositionMutationGate(path)
        self.causes = SqlitePendingWorkClaimStore(path / "causes.sqlite")
        self.intake = Mock(spec=CompletionIntakeLedger)
        self.intake.evidence_receive_sequence.return_value = 1
        self.events = InMemoryEventSink()
        self.compose()

    def compose(self, *, records=None):
        self.human = NeedsHumanBlock(
            "needs-human",
            "tech-lead-needs-human",
            self.remote,
            self.remote.read_issue_labels,
            frozenset,
            self.causes,
        )
        self.aggregate = AggregateRecoveryBlocks(
            repo_slug="owner/repo",
            records=self.base.store if records is None else records,
            admission=RankedEvidenceAdmission(self.base.store, self.intake),
            phases=self.base.phases,
            authority=self.base.effects,
            gate=self.gate,
            labels=LabelManager(Config(repo="owner/repo")),
            reader=self.remote,
            applier=self.remote,
            human_block=self.human,
        )
        self.abandonment = OperatorValidatedWorkAbandonment(
            repo_slug="owner/repo",
            store=self.base.store,
            execution=self.base.execution,
            gate=self.gate,
            blocks=self.aggregate,
            events=self.events,
        )
        self.base.finalizer = StagedPublishedWorkFinalizer(
            effects=self.base.effects,
            phases=self.base.phases,
            recovery=self.aggregate,
            fresh_issue_reader=self.remote,
            action_applier=self.remote,
            review_policy=RetryReviewPolicy(code_review_agent_configured=True),
            routing_label="pr-pending",
            clock=lambda: datetime.fromisoformat(AT),
        )

    def reopen(self):
        self.base.reopen()
        self.compose()


def assert_admission_busy_in_child(path: Path) -> None:
    script = """
from pathlib import Path
import sys
from unittest.mock import Mock
from issue_orchestrator.adapters.issue_disposition_gate import FileIssueDispositionMutationGate
from issue_orchestrator.control.aggregate_recovery_block import AggregateRecoveryBlocks
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.ports.event_sink import NullEventSink
from issue_orchestrator.domain.recovery_block import RecoveryMutationBusy
from issue_orchestrator.infra.config import Config
from tests.unit.validated_work_support import capture
admission = Mock()
owner = AggregateRecoveryBlocks(
    repo_slug='owner/repo', records=Mock(), admission=admission, phases=Mock(),
    authority=Mock(), gate=FileIssueDispositionMutationGate(Path(sys.argv[1])),
    labels=LabelManager(Config(repo='owner/repo')), reader=Mock(), applier=Mock(), human_block=Mock(),
)
try:
    owner.admit(capture(branch='new-arrival'))
except RecoveryMutationBusy:
    admission.admit.assert_not_called()
    print('busy')
else:
    raise AssertionError('child crossed the parent release gate')
"""
    result = run_in_process_group([sys.executable, "-c", script, str(path)], timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "busy"


def assert_human_acquisition_busy_in_child(path: Path) -> None:
    script = """
from pathlib import Path
import sys
from issue_orchestrator.control.needs_human_block import (
    NeedsHumanBlock, HumanBlockRequest, NeedsHumanCause, BlockOutcome,
)
from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore

def forbidden(*args):
    raise AssertionError('contender reached a boundary inside another owner scope')

class NoLabels:
    add_label = remove_label = forbidden

causes = SqlitePendingWorkClaimStore(Path(sys.argv[1]) / 'causes.sqlite')
owner = NeedsHumanBlock('needs-human', 'tech-lead-needs-human', NoLabels(),
                       forbidden, forbidden, causes)
result = owner.acquire(HumanBlockRequest(6914, NeedsHumanCause.SESSION_LIFECYCLE, 'new failure'))
assert result is BlockOutcome.FAILED
print('busy')
"""
    result = run_in_process_group([sys.executable, "-c", script, str(path)], timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "busy"

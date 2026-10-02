"""Tech-lead exam: grade the real system's OUTCOME on a planted fault (#7304).

A unit test checks a mechanism ("the rule fires"). An exam checks what the
system actually did about a known problem, against a known right answer:

* a *case* (:mod:`.case`) names the planted fault, the goal predicates on the
  final state, the known root cause, and the right remedy;
* an *observation* (:mod:`.observation`) is what the live harness saw when the
  run ended — GitHub state per work item, tech-lead runs, GitHub calls, time;
* the *grader* (:mod:`.grading`) turns the pair into a :class:`Scorecard`
  (:mod:`.scorecard`), machine-readable JSON plus a readable summary, including
  where every unfinished work item stalled.

Everything here is pure: the live harness under ``tests/e2e/exam/`` drives the
orchestrator and builds the observation, so grading and reporting are covered
by fast unit tests that run in the validation gate.
"""

from .case import (
    ExamCase,
    Goal,
    GoalCheck,
    RemedySpec,
    RootCauseSpec,
    TermGroup,
)
from .github_calls import EndpointClass, GitHubCallCounts, classify_command
from .grading import grade
from .observation import (
    ExamObservation,
    PullRequestFact,
    PullRequestState,
    RunEnd,
    StallFacts,
    TechLeadActionDisposition,
    TechLeadActionFact,
    TechLeadReceipt,
    TechLeadRunFact,
    TriageFact,
    WorkItemFact,
)
from .scorecard import Scorecard, render_summary
from .upgrade import LabelChange, UpgradeFacts, UpgradeGrade, UpgradeSpec, WriteKind

__all__ = [
    "EndpointClass",
    "ExamCase",
    "ExamObservation",
    "GitHubCallCounts",
    "Goal",
    "GoalCheck",
    "LabelChange",
    "PullRequestFact",
    "PullRequestState",
    "RemedySpec",
    "RootCauseSpec",
    "RunEnd",
    "Scorecard",
    "StallFacts",
    "TechLeadActionDisposition",
    "TechLeadActionFact",
    "TechLeadReceipt",
    "TechLeadRunFact",
    "TermGroup",
    "TriageFact",
    "UpgradeFacts",
    "UpgradeGrade",
    "UpgradeSpec",
    "WorkItemFact",
    "WriteKind",
    "classify_command",
    "grade",
    "render_summary",
]

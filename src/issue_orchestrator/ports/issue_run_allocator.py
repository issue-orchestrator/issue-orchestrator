"""Allocate exact artifacts and record their ownership before returning them."""

from typing import Protocol

from ..domain.issue_run_allocation import IssueExchangeRunAllocation, IssueRunAllocation
from ..domain.review_exchange_run import ReviewExchangeRun
from ..domain.session_run import SessionRunAssets, RunContainedFile


class IssueRunAllocator(Protocol):
    def allocate(self, request: IssueRunAllocation) -> SessionRunAssets: ...

    def allocate_exchange(self, request: IssueExchangeRunAllocation) -> ReviewExchangeRun: ...

    def submission_capability(self, run: SessionRunAssets) -> str: ...

    def submission_capability_file(self, run: SessionRunAssets) -> RunContainedFile: ...

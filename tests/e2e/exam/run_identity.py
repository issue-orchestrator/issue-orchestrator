"""One exam run's identity: its label and its engine checkout's name.

Both derive from ONE random id, so two runs — even of the same case in the
same second — can never share a label (cleanup selects by it: a shared
label would let one run close another's live issues and PRs) or a checkout
directory. No e2e imports, so this is unit-tested.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

#: Real engines exclude this prefix (``filtering.exclude_label_prefixes``),
#: and e2e reconciliation cleans up labels carrying it.
E2E_TEST_LABEL_PREFIX = "io:e2e:"


@dataclass(frozen=True)
class RunIdentity:
    case_id: str
    run_id: str

    @classmethod
    def new(cls, case_id: str) -> "RunIdentity":
        return cls(case_id=case_id, run_id=uuid.uuid4().hex[:12])

    @property
    def label(self) -> str:
        """The engine's filter label for this run, and its cleanup selector."""
        return f"{E2E_TEST_LABEL_PREFIX}exam-{self.case_id[:1].lower()}-{self.run_id}"

    def checkout_name(self, commit: str) -> str:
        return f"exam-engine-{commit[:10]}-{self.case_id[:1].lower()}-{self.run_id}"

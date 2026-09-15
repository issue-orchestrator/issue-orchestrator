#!/usr/bin/env python3
"""Compare a lifecycle plan's revisions against the projection an apply will publish.

Step 3 of #7240 says "stop if the plan is stale", but on a repository whose
shared registry has never been seeded the dry run CANNOT tell you: it refuses to
write, therefore refuses to seed, therefore reports every planned signature as
`unknown` (see docs/development/CASE_FILE_RECONCILIATION.md, "Not yet seeded").

That leaves the staleness check to be done by hand against the exact seed the
first apply will publish -- local evidence rows padded to `observation_count`
with synthetic `legacy:` identities, which are themselves part of the revision.
Doing it by hand is how #7249's first attempt shipped two revisions taken from
the un-padded local read.

This performs that comparison with the production seed code, so the revisions it
reports are the ones `require_reviewed_revision` will actually compare against.

    python scripts/check_lifecycle_plan_revisions.py \
        --plan repo-specific/reconciliation/tech-lead-case-lifecycle-porchpin-2026-09.yaml \
        --repo-root ~/dev/porchpin

Exit status is 0 only when every planned signature matches, the coverage is
exact, and every issue number agrees.

Run it IMMEDIATELY before the apply. The projection moves whenever the engine
records another observation, so a check from yesterday proves nothing about
today.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from issue_orchestrator.control.pattern_registry import (  # noqa: E402
    MirroredPatternCaseFileRegistry,
)
from issue_orchestrator.infra.tech_lead_authority_store import (  # noqa: E402
    SqliteTechLeadAuthorityStore,
)


def seeded_projection(repo_root: Path) -> dict[str, object]:
    """The entries a first apply would publish, by signature."""
    store = SqliteTechLeadAuthorityStore.for_repo(repo_root)
    # Never used for a write: `_seed` is a pure projection of one evidence row,
    # and this reads the trusted local ledger only.
    projector = MirroredPatternCaseFileRegistry(
        shared=None,  # type: ignore[arg-type]
        local=store,
        claimant_id="plan-revision-check",
    )
    return {
        entry.signature: entry
        for entry in (
            projector._seed(evidence)  # noqa: SLF001
            for evidence in store.list_pattern_evidence()
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        help="Checkout whose .issue-orchestrator/state holds the trusted ledger",
    )
    args = parser.parse_args()

    document = yaml.safe_load(args.plan.read_text(encoding="utf-8"))
    outcomes = {item["signature"]: item for item in document["outcomes"]}
    seeded = seeded_projection(args.repo_root.expanduser())

    print(f"plan       : {args.plan}")
    print(f"repository : {document.get('repository')}")
    print(f"plan_id    : {document.get('plan_id')}")
    print(f"planned    : {len(outcomes)}   local ledger: {len(seeded)}\n")

    problems: list[str] = []
    for signature in sorted(set(outcomes) - set(seeded)):
        problems.append(f"planned but absent from the ledger: {signature}")
    for signature in sorted(set(seeded) - set(outcomes)):
        problems.append(f"in the ledger but not covered by the plan: {signature}")

    moved = 0
    for signature, item in sorted(outcomes.items()):
        entry = seeded.get(signature)
        if entry is None:
            continue
        if entry.issue_number != item["issue"]:
            problems.append(
                f"{signature}: plan says #{item['issue']},"
                f" ledger says #{entry.issue_number}"
            )
        actual = entry.review_revision()
        if actual != item["expected_revision"]:
            moved += 1
            problems.append(
                f"{signature}: expected_revision is stale\n"
                f"    plan   : {item['expected_revision']}\n"
                f"    seeded : {actual}"
            )

    if problems:
        print(f"DRIFT ({moved} revision(s) moved):\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nRegenerate the plan from the seeded projection and re-review it."
            " Do not bypass the compare-and-swap."
        )
        return 1

    print("OK - every planned revision matches the projection an apply will publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

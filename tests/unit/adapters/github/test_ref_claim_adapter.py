"""Unit tests for the GitHub ref-backed claim adapter."""

from __future__ import annotations

from datetime import datetime, timedelta

from issue_orchestrator.adapters.github.errors import GitHubHttpError
from issue_orchestrator.adapters.github.claim_parser import format_claim_comment
from issue_orchestrator.adapters.github.ref_claim_adapter import (
    CLAIM_REF_PREFIX,
    GitHubRefClaimAdapter,
)
from issue_orchestrator.domain.claim import Claim, ClaimState
from issue_orchestrator.domain.lease_config import LeaseConfig


class FakeGitHubRefClient:
    """In-memory GitHub Git Database subset with fast-forward ref updates."""

    def __init__(self) -> None:
        self.refs: dict[str, str] = {"refs/heads/main": "base"}
        self.commits: dict[str, dict] = {
            "base": {
                "sha": "base",
                "message": "base commit",
                "tree": {"sha": "tree-base"},
                "parents": [],
            }
        }
        self.created_refs: list[tuple[str, str]] = []
        self.updated_refs: list[tuple[str, str, bool]] = []
        self.deleted_refs: list[str] = []
        self.conflict_updates_remaining = 0
        self.default_branch_reads = 0
        self._next_commit = 1

    def get_default_branch(self) -> str:
        self.default_branch_reads += 1
        return "main"

    def get_git_ref(self, ref: str) -> dict | None:
        sha = self.refs.get(ref)
        if sha is None:
            return None
        return {"ref": ref, "object": {"type": "commit", "sha": sha}}

    def create_git_ref(self, *, ref: str, sha: str) -> dict:
        if ref in self.refs:
            raise GitHubHttpError("ref exists", status_code=422)
        self.refs[ref] = sha
        self.created_refs.append((ref, sha))
        return {"ref": ref, "object": {"type": "commit", "sha": sha}}

    def update_git_ref(self, *, ref: str, sha: str, force: bool = False) -> dict:
        if self.conflict_updates_remaining:
            self.conflict_updates_remaining -= 1
            raise GitHubHttpError("conflict", status_code=409)
        current_sha = self.refs[ref]
        parents = self.commits[sha]["parents"]
        parent_sha = parents[0]["sha"] if parents else None
        if not force and parent_sha != current_sha:
            raise GitHubHttpError("not fast-forward", status_code=409)
        self.refs[ref] = sha
        self.updated_refs.append((ref, sha, force))
        return {"ref": ref, "object": {"type": "commit", "sha": sha}}

    def delete_git_ref(self, ref: str) -> None:
        if ref not in self.refs:
            raise GitHubHttpError("ref not found", status_code=404)
        del self.refs[ref]
        self.deleted_refs.append(ref)

    def get_git_commit(self, sha: str) -> dict:
        return self.commits[sha]

    def create_git_commit(
        self,
        *,
        message: str,
        tree_sha: str,
        parents: list[str],
    ) -> dict:
        sha = f"commit-{self._next_commit}"
        self._next_commit += 1
        self.commits[sha] = {
            "sha": sha,
            "message": message,
            "tree": {"sha": tree_sha},
            "parents": [{"sha": parent} for parent in parents],
        }
        return self.commits[sha]

    def seed_claim_ref(self, issue_number: int, claim: Claim) -> None:
        commit = self.create_git_commit(
            message=format_claim_comment(claim),
            tree_sha="tree-base",
            parents=["base"],
        )
        self.create_git_ref(
            ref=f"{CLAIM_REF_PREFIX}/issue-{issue_number}",
            sha=commit["sha"],
        )


class FakeLabels:
    def __init__(self) -> None:
        self.added: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []

    def add_label(self, issue_number: int, label: str) -> None:
        self.added.append((issue_number, label))

    def remove_label(self, issue_number: int, label: str) -> None:
        self.removed.append((issue_number, label))


def _adapter(
    client: FakeGitHubRefClient,
    labels: FakeLabels,
    claimant_id: str,
    *,
    lease_seconds: int = 30,
) -> GitHubRefClaimAdapter:
    return GitHubRefClaimAdapter(
        client=client,
        claimant_id=claimant_id,
        config=LeaseConfig(
            lease_seconds=lease_seconds,
            renew_interval_seconds=10,
            convergence_timeout_seconds=0.1,
            convergence_poll_min_ms=1,
            convergence_poll_max_ms=1,
        ),
        label_adapter=labels,
    )


def test_attempt_claim_creates_issue_ref_and_label() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    result = adapter.attempt_claim(issue_number=42)

    assert result.success is True
    assert result.state == ClaimState.CLAIMED
    assert result.lease_id is not None
    assert client.created_refs[0][0] == f"{CLAIM_REF_PREFIX}/issue-42"
    assert labels.added == [(42, "io:claimed")]
    assert adapter.run_convergence(42, result.lease_id) is True


def test_second_adapter_loses_to_active_ref_claim() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter_a = _adapter(client, labels, "orchestrator-a")
    adapter_b = _adapter(client, labels, "orchestrator-b")

    first = adapter_a.attempt_claim(issue_number=42)
    second = adapter_b.attempt_claim(issue_number=42)

    assert first.success is True
    assert second.success is False
    assert second.state == ClaimState.CLAIM_LOST
    assert second.competing_claims[0].claimant == "orchestrator-a"
    assert "already claimed" in str(second.error)
    assert labels.added == [(42, "io:claimed")]


def test_released_claim_can_be_taken_over_by_another_adapter() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter_a = _adapter(client, labels, "orchestrator-a")
    adapter_b = _adapter(client, labels, "orchestrator-b")

    first = adapter_a.attempt_claim(issue_number=42)
    assert first.lease_id is not None
    adapter_a.release_claim(issue_number=42, lease_id=first.lease_id)
    assert client.deleted_refs == [f"{CLAIM_REF_PREFIX}/issue-42"]
    assert client.get_git_ref(f"{CLAIM_REF_PREFIX}/issue-42") is None

    second = adapter_b.attempt_claim(issue_number=42)

    assert second.success is True
    current = adapter_b.get_current_claim(42)
    assert current is not None
    assert current.lease_id == second.lease_id
    assert current.claimant == "orchestrator-b"
    assert labels.removed == [(42, "io:claimed")]


def test_release_ignores_non_matching_lease() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    result = adapter.attempt_claim(issue_number=42)
    adapter.release_claim(issue_number=42, lease_id="someone-else")

    assert result.lease_id is not None
    assert client.deleted_refs == []
    current = adapter.get_current_claim(42)
    assert current is not None
    assert current.lease_id == result.lease_id
    assert labels.removed == []


def test_default_branch_is_cached_for_first_time_claims() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    first = adapter.attempt_claim(issue_number=42)
    second = adapter.attempt_claim(issue_number=43)

    assert first.success is True
    assert second.success is True
    assert client.default_branch_reads == 1


def test_renew_claim_moves_ref_with_non_force_update() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    result = adapter.attempt_claim(issue_number=42)
    assert result.lease_id is not None
    renewed = adapter.renew_claim(issue_number=42, lease_id=result.lease_id)

    assert renewed is True
    assert client.updated_refs[-1][0] == f"{CLAIM_REF_PREFIX}/issue-42"
    assert client.updated_refs[-1][2] is False
    current = adapter.get_current_claim(42)
    assert current is not None
    assert current.lease_id == result.lease_id


def test_renew_claim_returns_false_on_cas_conflict() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")

    result = adapter.attempt_claim(issue_number=42)
    assert result.lease_id is not None
    client.conflict_updates_remaining = 1

    assert adapter.renew_claim(issue_number=42, lease_id=result.lease_id) is False


def test_expired_claim_is_not_current() -> None:
    client = FakeGitHubRefClient()
    labels = FakeLabels()
    adapter = _adapter(client, labels, "orchestrator-a")
    expired_claim = Claim(
        lease_id="lease-expired",
        claimant="orchestrator-a",
        issue_number=42,
        started_at=datetime.now() - timedelta(seconds=10),
        expires_at=datetime.now() - timedelta(seconds=1),
        priority=1,
    )
    client.seed_claim_ref(42, expired_claim)

    assert adapter.get_current_claim(42) is None

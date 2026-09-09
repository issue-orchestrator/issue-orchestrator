"""Exact detached workspaces reconstructed without the original coding worktree."""

import json
import uuid
from dataclasses import asdict
from pathlib import Path

from ..domain.publication_workspace import PublicationWorkspace
from ..domain.validated_work_escrow import (
    ARTIFACT_FILENAMES, EscrowArtifacts, VerifiedEscrowCapture, evidence_artifacts,
    validate_capture_locations,
)
from ..domain.validated_work_store import EvidenceAdmission
from ..infra.escrow_files import durable_directory, fsync_directory, read_regular, publish_durable
from ..ports.git import Git, GitError
from ..ports.validated_work_escrow import ValidatedWorkEscrow
from .publication_checkout_integrity import require_exact_checkout_content


class EscrowPublicationWorkspaces:
    """One owner for allocation, identity checks, crash recovery, and removal.

    Public operations run under the disposition issue gate held by the caller.
    The write-ahead receipt permits replay of partial creation/removal, but never
    grants authority to replace changed artifacts or a dirty/foreign checkout.
    Capture bytes and retention pins survive release of publication assets.
    """

    def __init__(self, *, root: Path, repository: Path, repo_slug: str,
                 escrow: ValidatedWorkEscrow, git: Git) -> None:
        self._root = root.absolute()
        self._repository = repository.resolve()
        self._repo_slug = repo_slug
        self._escrow = escrow
        self._git = git
        self._canonical(self._root)
        self._common = self._common_directory(self._repository)

    def prepare(self, admission: EvidenceAdmission) -> PublicationWorkspace:
        capture, workspace = self._capture(admission)
        self._escrow.verify_pins(capture.admission)
        directory = workspace.checkout.parent
        receipt = directory.parent / "publication-owner.json"
        if not receipt.exists():
            if directory.exists():
                raise ValueError("unowned publication directory retained")
            publish_durable(receipt, self._receipt(workspace), staging_root=self._root / ".tmp")
            fsync_directory(directory.parent)
        self._verify_owner(workspace)
        durable_directory(directory)
        self._prepare_artifacts(capture, workspace)
        if not workspace.checkout.exists():
            self._remove_absent_registration(workspace.checkout)
            self._create_verified_checkout(workspace)
        self._verify_checkout(workspace.checkout, workspace.key.validated_head_sha)
        # A process may have died after the atomic move but before Git updated
        # its reverse registration. The exact checkout is verified first.
        self._git.repair_worktree_registration(self._repository, workspace.checkout)
        return workspace

    def release(self, admission: EvidenceAdmission) -> None:
        capture, workspace = self._capture(admission)
        directory = workspace.checkout.parent
        receipt = directory.parent / "publication-owner.json"
        if not receipt.exists() and not directory.exists():
            return
        self._verify_owner(workspace)
        self._verify_artifacts(capture, workspace, allow_missing=True)
        if workspace.checkout.exists():
            self._verify_checkout(workspace.checkout, workspace.key.validated_head_sha)
            self._git.repair_worktree_registration(self._repository, workspace.checkout)
            # No force and no broad prune: an operator's changed work survives.
            self._git.run(self._repository, ["worktree", "remove", "--", str(workspace.checkout)])
        else:
            self._remove_absent_registration(workspace.checkout)
        run = workspace.artifacts.completion.parent
        if run.exists():
            for artifact in evidence_artifacts(capture.admission.evidence):
                path = workspace.artifacts.for_slot(artifact.slot)
                if path.exists():
                    path.unlink()
            run.rmdir()
        if directory.exists():
            directory.rmdir()
            fsync_directory(directory.parent)
        receipt.unlink()
        fsync_directory(directory.parent)

    def _capture(self, admission: EvidenceAdmission) -> tuple[VerifiedEscrowCapture, PublicationWorkspace]:
        validate_capture_locations(admission)
        if admission.evidence.identity.key.repo_slug != self._repo_slug:
            raise ValueError("publication belongs to another repository")
        capture = self._escrow.read_capture(admission.escrow_dir)
        if capture.admission.evidence.identity != admission.evidence.identity:
            raise ValueError("publication capture does not match admitted evidence")
        directory = self._root / admission.escrow_dir / "publication"
        self._canonical(directory)
        if not directory.is_relative_to(self._root):
            raise ValueError("publication directory escapes the escrow root")
        run = directory / "run"
        return capture, PublicationWorkspace(
            admission.evidence.identity.key, admission.evidence.evidence_id,
            directory / "workspace", EscrowArtifacts(
                run / "completion.json", run / "validation.json",
                run / "exchange-summary.md" if capture.exchange_summary is not None else None,
            ),
        )

    def _receipt(self, workspace: PublicationWorkspace) -> bytes:
        return json.dumps({
            "version": 1, "repository": str(self._common),
            "key": asdict(workspace.key), "evidence_id": workspace.evidence_id,
            "checkout": str(workspace.checkout),
        }, sort_keys=True).encode()

    def _verify_owner(self, workspace: PublicationWorkspace) -> None:
        directory = workspace.checkout.parent
        self._canonical(directory)
        receipt = self._receipt(workspace)
        if read_regular(directory.parent / "publication-owner.json", len(receipt)) != receipt:
            raise ValueError("publication ownership receipt does not match")
        if directory.exists() and {path.name for path in directory.iterdir()} - {"workspace", "run"}:
            raise ValueError("unknown publication contents retained")
        for path in (workspace.checkout, workspace.artifacts.completion.parent):
            self._canonical(path)

    def _prepare_artifacts(self, capture: VerifiedEscrowCapture, workspace: PublicationWorkspace) -> None:
        self._verify_artifacts(capture, workspace, allow_missing=True)
        run = workspace.artifacts.completion.parent
        durable_directory(run)
        for artifact in evidence_artifacts(capture.admission.evidence):
            path = workspace.artifacts.for_slot(artifact.slot)
            if not path.exists():
                publish_durable(path, capture.for_slot(artifact.slot), staging_root=self._root / ".tmp")
        fsync_directory(run)
        self._verify_artifacts(capture, workspace, allow_missing=False)

    def _verify_artifacts(self, capture: VerifiedEscrowCapture, workspace: PublicationWorkspace,
                          *, allow_missing: bool) -> None:
        run = workspace.artifacts.completion.parent
        self._canonical(run)
        if not run.exists() and allow_missing:
            return
        expected = {ARTIFACT_FILENAMES[artifact.slot] for artifact in evidence_artifacts(capture.admission.evidence)}
        if {path.name for path in run.iterdir()} - expected:
            raise ValueError("unknown publication run contents retained")
        for artifact in evidence_artifacts(capture.admission.evidence):
            path = workspace.artifacts.for_slot(artifact.slot)
            self._canonical(path)
            if not path.exists() and allow_missing:
                continue
            if read_regular(path, artifact.byte_size) != capture.for_slot(artifact.slot):
                raise ValueError("modified publication artifact retained")

    def _create_verified_checkout(self, workspace: PublicationWorkspace) -> None:
        staging = self._root / ".tmp" / uuid.uuid4().hex
        durable_directory(staging)
        checkout = staging / "workspace"
        target = workspace.key.validated_head_sha
        self._git.run(self._repository, ["worktree", "add", "--detach", str(checkout), target])
        self._verify_checkout(checkout, target)
        # Only complete, verified work becomes the authoritative workspace.
        # An interrupted checkout stays in private staging and is never reused
        # or discarded on a guess that it contains no operator edits.
        self._git.run(self._repository, ["worktree", "move", str(checkout), str(workspace.checkout)])
        fsync_directory(workspace.checkout.parent)
        staging.rmdir()
        fsync_directory(staging.parent)

    def _verify_checkout(self, checkout: Path, target: str) -> None:
        self._canonical(checkout)
        if self._common_directory(checkout) != self._common:
            raise ValueError("publication checkout belongs to another repository")
        top = Path(self._git.run(checkout, ["rev-parse", "--show-toplevel"]).stdout.strip())
        if top != checkout:
            raise ValueError("publication checkout points at another worktree")
        branch = self._git.run(checkout, ["symbolic-ref", "--quiet", "HEAD"], check=False)
        if branch.returncode == 0:
            raise ValueError("publication checkout must remain detached")
        if branch.returncode != 1:
            raise GitError(branch)
        if self._git.head_sha(checkout) != target:
            raise ValueError("publication checkout HEAD changed; retained")
        status = self._git.run(checkout, ["status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"])
        if status.stdout:
            raise ValueError("dirty publication checkout retained")
        require_exact_checkout_content(self._git, checkout, target)

    def _common_directory(self, path: Path) -> Path:
        return Path(self._git.run(path, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()).resolve()

    def _remove_absent_registration(self, checkout: Path) -> None:
        self._canonical(checkout)
        if checkout.exists():
            raise ValueError("cannot remove registration for a present checkout")
        registered = self._git.run(self._repository, ["worktree", "list", "--porcelain", "-z"]).stdout.split("\0")
        if f"worktree {checkout}" in registered:
            self._git.run(self._repository, ["worktree", "remove", "--force", "--", str(checkout)])

    @staticmethod
    def _canonical(path: Path) -> None:
        if path.resolve() != path:
            raise ValueError("publication paths must be canonical without symlinks")

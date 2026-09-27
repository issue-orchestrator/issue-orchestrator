"""Exact detached workspaces reconstructed without the original coding worktree."""

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from ..adapters.worktree.custody import custody_guard
from ..adapters.worktree.removal import GitRunner, remove_checkout_path
from ..domain.completion_intake import CompletionIntakeError
from ..domain.publication_workspace import PublicationWorkspace
from ..domain.validated_work_escrow import (
    ARTIFACT_FILENAMES, EscrowArtifacts, VerifiedEscrowCapture, evidence_artifacts,
    publication_locator,
    validate_capture_locations,
)
from ..domain.validated_work_store import EvidenceAdmission
from ..infra.escrow_files import durable_directory, fsync_directory, read_regular, publish_durable
from ..infra.runtime_artifacts import (
    CLEANUP_SAFE_UNTRACKED_ROOTS,
    DEPENDENCY_OUTPUT_DIR_NAMES,
    builtin_cleanup_root,
)
from ..ports.command_runner import OutputNewlines
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
                 escrow: ValidatedWorkEscrow, git: Git,
                 prepare: Callable[[Path], None]) -> None:
        self._root = root.absolute()
        self._repository = repository.resolve()
        self._repo_slug = repo_slug
        self._escrow = escrow
        self._git = git
        self._prepare = prepare
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
            durable_directory(directory.parent)
            publish_durable(receipt, self._receipt(workspace), staging_root=self._root / ".tmp")
            fsync_directory(directory.parent)
        self._verify_owner(workspace)
        durable_directory(directory)
        self._prepare_artifacts(capture, workspace)
        if not workspace.checkout.exists():
            # Nothing to protect at a path with no checkout: a grant naming one
            # is a breach, and `breached()` is what reports it.
            self._remove_absent_registration(workspace.checkout)
            self._create_verified_checkout(workspace)
        # Verification FIRST, so a foreign repository planted here still gets
        # its own precise diagnostic rather than a custody refusal.
        self._verify_checkout(workspace.checkout, workspace.key.validated_head_sha)
        # From here a retry repairs the registration, clears runtime directories
        # and runs setup -- all destructive, and all BEFORE this path reaches
        # its own removal guard (round 22 finding 1).
        with custody_guard(workspace.checkout, repo_root=self._repository):
            # A process may have died after the atomic move but before Git
            # updated its reverse registration. The exact checkout is verified
            # first.
            self._git.repair_worktree_registration(self._repository, workspace.checkout)
            self._clear_setup_outputs(
                workspace.checkout, workspace.key.validated_head_sha
            )
            self._require_setup_roots_local(workspace.checkout)
            try:
                self._prepare(workspace.checkout)
            except (OSError, RuntimeError) as exc:
                raise CompletionIntakeError(
                    "publication workspace setup failed"
                ) from exc
            self._verify_checkout(workspace.checkout, workspace.key.validated_head_sha)
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
            self._verify_checkout(
                workspace.checkout, workspace.key.validated_head_sha
            )
            with custody_guard(workspace.checkout, repo_root=self._repository):
                self._git.repair_worktree_registration(
                    self._repository, workspace.checkout
                )
                # Verification admits only exact source plus explicitly owned
                # runtime/dependency output. Git requires force for those paths.
                remove_checkout_path(
                    workspace.checkout,
                    force=True,
                    run_git=self._git_runner(),
                    repo_root=self._repository,
                )
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
        directory = self._root / publication_locator(admission.evidence) / "publication"
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
        # Moving a held checkout leaves the grant naming a path nothing is at,
        # so the next removal of the NEW path is unheld (round 21 finding 1).
        with custody_guard(checkout, repo_root=self._repository):
            self._git.run(
                self._repository,
                ["worktree", "move", str(checkout), str(workspace.checkout)],
            )
        fsync_directory(workspace.checkout.parent)
        staging.rmdir()
        fsync_directory(staging.parent)

    def _verify_checkout(self, checkout: Path, target: str,
                         *, allow_setup_outputs: bool = True) -> None:
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
        tracked = self._git.run(checkout, ["status", "--porcelain=v1", "-z",
                                          "--untracked-files=no"],
                                newlines=OutputNewlines.PRESERVED)
        if tracked.stdout.strip("\0"):
            raise ValueError("dirty publication checkout retained")
        outputs = self._untracked_outputs(checkout)
        if any(root is None or not allow_setup_outputs for root in outputs.values()):
            raise ValueError("dirty publication checkout retained")
        require_exact_checkout_content(self._git, checkout, target)

    def _untracked_outputs(self, checkout: Path) -> dict[str, str | None]:
        """Map every untracked path to the output root that owns it, or None.

        The checkout's only source of truth is the exact validated commit, which
        escrow pins independently; nothing untracked is ever published. Two
        owners are trusted to have produced untracked paths here:

        - process policy (``builtin_cleanup_root``): runtime and dependency
          output io's own setup writes; and
        - the validated commit's committed ignore rules: output the repository
          itself declares generated. This is what its pre-push hook leaves
          when io pushes from this checkout (#7346/#7289) -- ``.build/``,
          ``*.tsbuildinfo``, test results. It is not operator work.

        Ignore status is evaluated from per-directory ``.gitignore`` files
        ONLY. The operator's own excludes (``info/exclude``,
        ``core.excludesFile``) never grant cleanup authority, so a file an
        operator hid with them is reported unowned and preserved. Tracked
        ``.gitignore`` content is proven exact by
        ``require_exact_checkout_content``; an UNTRACKED ``.gitignore`` could
        grant itself authority, so it is never owned (outside a builtin root).
        A tracked modification, a moved HEAD or an untracked non-ignored file
        stays unowned: that is what stranded work looks like, and it is kept.
        """
        rules = ["--exclude-per-directory=.gitignore"]
        visible = self._git.run(checkout, ["ls-files", "-z", "--others", *rules],
                                newlines=OutputNewlines.PRESERVED).stdout
        ignored = self._git.run(
            checkout, ["ls-files", "-z", "--others", "--ignored", "--directory", *rules],
            newlines=OutputNewlines.PRESERVED,
        ).stdout
        outputs: dict[str, str | None] = {
            path: builtin_cleanup_root(path) for path in visible.split("\0") if path
        }
        for path in ignored.split("\0"):
            if not path:
                continue
            owned = path.rstrip("/")
            outputs[path] = builtin_cleanup_root(path) or (
                None if Path(owned).name == ".gitignore" else owned
            )
        return outputs

    def _clear_setup_outputs(self, checkout: Path, target: str) -> None:
        """Reset trusted generated paths without following planted symlinks."""
        roots: set[Path] = set()
        for root in self._untracked_outputs(checkout).values():
            if root is None:
                raise ValueError("dirty publication checkout retained")
            relative = Path(root)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("publication setup output escapes checkout")
            roots.add(relative)
        selected = tuple(
            root for root in sorted(roots, key=lambda item: (len(item.parts), str(item)))
            if not any(root.is_relative_to(parent) for parent in roots if parent != root)
        )
        if selected:
            # Git owns the tracked/untracked boundary: even when a generated
            # child expands to a runtime root, clean preserves tracked siblings
            # and descendants. It also unlinks symlinks instead of traversing
            # their targets.
            self._git.run(checkout, ["clean", "-fdx", "--", *(str(path) for path in selected)])
        # Setup always starts from exact committed source. This both bounds the
        # callback and proves cleanup did not traverse a redirected path.
        self._verify_checkout(checkout, target, allow_setup_outputs=False)

    @staticmethod
    def _require_setup_roots_local(checkout: Path) -> None:
        """Reject committed redirects at paths dependency setup may write."""
        for root in CLEANUP_SAFE_UNTRACKED_ROOTS:
            candidate = checkout
            for part in Path(root).parts:
                candidate /= part
                if candidate.is_symlink():
                    raise ValueError("publication setup root redirects outside workspace")
                if not candidate.exists():
                    break
        for candidate in checkout.rglob("*"):
            if candidate.name in DEPENDENCY_OUTPUT_DIR_NAMES and candidate.is_symlink():
                raise ValueError("publication setup root redirects outside workspace")

    def _common_directory(self, path: Path) -> Path:
        return Path(self._git.run(path, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()).resolve()

    def _git_runner(self) -> GitRunner:
        def run(argv: list[str]) -> str | None:
            try:
                self._git.run(self._repository, argv)
            except GitError as exc:
                return str(exc)
            return None

        return run

    def _remove_absent_registration(self, checkout: Path) -> None:
        self._canonical(checkout)
        if checkout.exists():
            raise ValueError("cannot remove registration for a present checkout")
        registered = self._git.run(self._repository, ["worktree", "list", "--porcelain", "-z"]).stdout.split("\0")
        if f"worktree {checkout}" in registered:
            remove_checkout_path(
                checkout,
                force=True,
                run_git=self._git_runner(),
                repo_root=self._repository,
            )

    @staticmethod
    def _canonical(path: Path) -> None:
        if path.resolve() != path:
            raise ValueError("publication paths must be canonical without symlinks")

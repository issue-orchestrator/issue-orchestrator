"""Publication recovery uses real escrow and Git, without original session assets."""

import shutil
from dataclasses import replace

import pytest

from issue_orchestrator.domain.completion_intake import CompletionIntakeError
from issue_orchestrator.execution.publication_workspace import EscrowPublicationWorkspaces
from issue_orchestrator.domain.validated_work_escrow import evidence_pins, publication_locator
from .git_escrow_support import git_rig, real_capture
from .test_validated_work_escrow import escrow_for


def publication_root(escrow):
    return escrow.root.parent / "validated-work-publications"


def publication_directory(escrow, admission):
    return publication_root(escrow) / publication_locator(admission.evidence) / "publication"


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    rig = git_rig(tmp_path)
    original = tmp_path / "original"
    rig.run("worktree", "add", "--detach", str(original), rig.target)
    admission, sources = real_capture(rig, original / "run")
    escrow = escrow_for(rig)
    escrow.capture(admission, sources)
    before = escrow.read_capture(admission.escrow_dir)
    rig.run("worktree", "remove", "--force", str(original))
    owner = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
                                       repo_slug="owner/repo", escrow=escrow, git=rig.git, prepare=lambda _: None)
    return rig, admission, escrow, owner, before


def test_reconstructs_exact_detached_work_and_verified_artifacts_after_original_is_gone(prepared):
    rig, admission, escrow, owner, capture = prepared
    workspace = owner.prepare(admission)
    assert all(":" not in part for part in workspace.checkout.parts)
    assert rig.git.head_sha(workspace.checkout) == rig.target
    assert rig.git.run(workspace.checkout, ["symbolic-ref", "-q", "HEAD"], check=False).returncode == 1
    assert workspace.artifacts.completion.read_bytes() == capture.completion
    assert workspace.artifacts.validation.read_bytes() == capture.validation
    assert workspace.artifacts.exchange_summary is None
    assert not rig.git.status_porcelain(workspace.checkout)
    assert owner.prepare(admission) == workspace
    assert rig.run("rev-parse", "HEAD") == rig.tip
    owner.release(admission)
    assert not workspace.checkout.parent.exists()
    assert not (workspace.checkout.parent.parent / "publication-owner.json").exists()
    assert escrow.read_capture(admission.escrow_dir) == capture
    escrow.verify_pins(admission)
    owner.release(admission)


def test_prepares_runtime_and_releases_owned_setup_output(prepared):
    rig, admission, escrow, _, _ = prepared
    prepared_paths = []

    def prepare_runtime(workspace):
        prepared_paths.append(workspace)
        runtime = workspace / ".venv"
        runtime.mkdir(exist_ok=True)
        (runtime / "ready").write_text("prepared")

    owner = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
        repo_slug="owner/repo", escrow=escrow, git=rig.git, prepare=prepare_runtime)
    workspace = owner.prepare(admission)
    assert prepared_paths == [workspace.checkout]
    assert (workspace.checkout / ".venv" / "ready").read_text() == "prepared"
    owner.release(admission)
    assert not workspace.checkout.exists()


def test_failed_runtime_setup_is_retryable_without_weakening_source_checks(prepared):
    rig, admission, escrow, _, _ = prepared
    attempts = 0

    def prepare_runtime(workspace):
        nonlocal attempts
        attempts += 1
        (workspace / ".venv").mkdir(exist_ok=True)
        if attempts == 1:
            raise RuntimeError("dependency service unavailable")

    owner = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
        repo_slug="owner/repo", escrow=escrow, git=rig.git, prepare=prepare_runtime)
    with pytest.raises(CompletionIntakeError, match="workspace setup failed"):
        owner.prepare(admission)
    workspace = owner.prepare(admission)
    assert attempts == 2
    owner.release(admission)


def test_mutable_runtime_ignore_cannot_authorize_publication_cleanup(prepared):
    _, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    ignore = workspace.checkout / ".issue-orchestrator" / "runtime-ignore"
    ignore.parent.mkdir(exist_ok=True)
    ignore.write_text("*\n")
    operator_work = workspace.checkout / "operator-work"
    operator_work.write_text("preserve")

    for operation in (owner.prepare, owner.release):
        with pytest.raises(ValueError, match="dirty publication checkout"):
            operation(admission)
        assert operator_work.read_text() == "preserve"


def test_prepare_unlinks_redirected_runtime_root_before_setup(prepared, tmp_path):
    rig, admission, escrow, owner, _ = prepared
    workspace = owner.prepare(admission)
    external = tmp_path / "external-runtime"
    external.mkdir()
    runtime = workspace.checkout / ".venv"
    runtime.symlink_to(external, target_is_directory=True)

    def prepare_runtime(checkout):
        runtime_root = checkout / ".venv"
        runtime_root.mkdir()
        (runtime_root / "ready").write_text("prepared")

    retrying = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
        repo_slug="owner/repo", escrow=escrow, git=rig.git, prepare=prepare_runtime)
    assert retrying.prepare(admission) == workspace
    assert not (external / "ready").exists()
    assert not runtime.is_symlink()
    assert (runtime / "ready").read_text() == "prepared"
    retrying.release(admission)


def test_prepare_refuses_committed_redirected_runtime_root(prepared, tmp_path):
    rig, _, escrow, _, _ = prepared
    external = tmp_path / "external-runtime"
    external.mkdir()
    (rig.root / ".venv").symlink_to(external, target_is_directory=True)
    rig.run("add", ".venv")
    rig.run("commit", "-m", "track redirected runtime root")
    rig.target = rig.run("rev-parse", "HEAD")
    admission, sources = real_capture(rig, tmp_path / "redirect-source")
    escrow.capture(admission, sources)
    setup_called = False

    def prepare_runtime(checkout):
        nonlocal setup_called
        setup_called = True
        (checkout / ".venv" / "escaped").write_text("unsafe")

    owner = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
        repo_slug="owner/repo", escrow=escrow, git=rig.git, prepare=prepare_runtime)
    with pytest.raises(ValueError, match="setup root redirects"):
        owner.prepare(admission)
    assert not setup_called
    assert not (external / "escaped").exists()


def test_retry_cleanup_preserves_tracked_files_in_mixed_runtime_root(prepared, tmp_path):
    rig, _, escrow, _, _ = prepared
    dist = rig.root / "packages" / "vscode" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.js").write_text("tracked")
    rig.run("add", "packages/vscode/dist/index.js", "-f")
    rig.run("commit", "-m", "track generated-directory input")
    rig.target = rig.run("rev-parse", "HEAD")
    admission, sources = real_capture(rig, tmp_path / "mixed-root-source")
    escrow.capture(admission, sources)

    def prepare_runtime(checkout):
        generated = checkout / "packages" / "vscode" / "dist" / "generated.cache"
        generated.write_text("generated")

    owner = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
        repo_slug="owner/repo", escrow=escrow, git=rig.git, prepare=prepare_runtime)
    workspace = owner.prepare(admission)
    assert owner.prepare(admission) == workspace
    assert (workspace.checkout / "packages/vscode/dist/index.js").read_text() == "tracked"
    assert (workspace.checkout / "packages/vscode/dist/generated.cache").read_text() == "generated"
    owner.release(admission)


def test_prepare_refuses_runtime_output_that_git_clean_cannot_remove(prepared, tmp_path):
    rig, admission, escrow, owner, _ = prepared
    workspace = owner.prepare(admission)
    external = tmp_path / "external-runtime"
    external.mkdir()
    runtime = workspace.checkout / ".venv"
    runtime.mkdir()
    rig.git.run(runtime, ["init"])
    (runtime / "lib").symlink_to(external, target_is_directory=True)
    setup_called = False

    def prepare_runtime(checkout):
        nonlocal setup_called
        setup_called = True
        (checkout / ".venv" / "lib" / "escaped").write_text("unsafe")

    retrying = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
        repo_slug="owner/repo", escrow=escrow, git=rig.git, prepare=prepare_runtime)
    with pytest.raises(ValueError, match="dirty publication checkout"):
        retrying.prepare(admission)
    assert not setup_called
    assert not (external / "escaped").exists()
    assert runtime.exists()


@pytest.mark.parametrize("damage", ["tracked", "untracked", "ignored", "head", "branch", "artifact", "unknown-run", "unknown-root"])
def test_modified_publication_work_is_preserved_on_prepare_and_release(prepared, damage, tmp_path):
    rig, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    if damage == "tracked":
        (workspace.checkout / "file").write_text("operator edit")
    elif damage in {"untracked", "ignored"}:
        (workspace.checkout / "operator-work").write_text("operator edit")
        if damage == "ignored":
            ignore_rules = tmp_path / "fixture-ignore-rules"
            ignore_rules.write_text("operator-work\n")
            rig.run("config", "core.excludesFile", str(ignore_rules))
    elif damage == "head":
        rig.git.run(workspace.checkout, ["checkout", "--detach", rig.tip])
    elif damage == "branch":
        rig.git.run(workspace.checkout, ["checkout", "-b", "operator-branch"])
    elif damage == "artifact":
        workspace.artifacts.completion.write_bytes(b"x" * len(workspace.artifacts.completion.read_bytes()))
    elif damage == "unknown-run":
        (workspace.artifacts.completion.parent / "operator-work").write_text("keep")
    else:
        (workspace.checkout.parent / "operator-work").write_text("keep")
    for operation in (owner.prepare, owner.release):
        with pytest.raises(ValueError):
            operation(admission)
        assert workspace.checkout.exists()
        assert workspace.artifacts.completion.exists()


@pytest.mark.parametrize("location", ["workspace", "run", "completion.json"])
def test_symlink_in_owned_paths_is_refused_without_touching_target(prepared, tmp_path, location):
    _, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    target = tmp_path / "operator-content"
    if location == "completion.json":
        path = workspace.artifacts.completion
        target.write_bytes(path.read_bytes())
        path.unlink()
    else:
        path = workspace.checkout if location == "workspace" else workspace.artifacts.completion.parent
        shutil.move(str(path), target)
    path.symlink_to(target)
    for operation in (owner.prepare, owner.release):
        with pytest.raises(ValueError, match="symlinks"):
            operation(admission)
    assert target.exists()


def test_stale_missing_workspace_registration_is_repaired_only_for_owned_path(prepared, tmp_path):
    rig, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    other = tmp_path / "other-missing-worktree"
    rig.run("worktree", "add", "--detach", str(other), rig.tip)
    shutil.rmtree(other)
    shutil.rmtree(workspace.checkout)
    assert owner.prepare(admission) == workspace
    assert rig.git.head_sha(workspace.checkout) == rig.target
    assert str(other) in rig.run("worktree", "list", "--porcelain")
    owner.release(admission)
    assert str(other) in rig.run("worktree", "list", "--porcelain")


@pytest.mark.parametrize("phase", ["receipt-only", "partial-run", "workspace-removed", "directory-removed"])
def test_owned_partial_creation_or_release_can_resume(prepared, phase):
    rig, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    rig.git.run(rig.root, ["worktree", "remove", str(workspace.checkout)])
    if phase in {"receipt-only", "directory-removed"}:
        shutil.rmtree(workspace.checkout.parent)
    elif phase == "partial-run":
        workspace.artifacts.validation.unlink()
    if phase == "directory-removed":
        owner.release(admission)
        assert not (workspace.checkout.parent.parent / "publication-owner.json").exists()
    else:
        assert owner.prepare(admission) == workspace
        owner.release(admission)
    assert not workspace.checkout.exists()


def test_unowned_directory_is_not_adopted_or_removed(prepared):
    _, admission, escrow, owner, _ = prepared
    directory = publication_directory(escrow, admission)
    directory.parent.mkdir(parents=True)
    directory.mkdir()
    (directory / "operator-work").write_text("keep")
    for operation in (owner.prepare, owner.release):
        with pytest.raises((ValueError, FileNotFoundError)):
            operation(admission)
    assert (directory / "operator-work").read_text() == "keep"


def test_missing_pin_prevents_workspace_creation(prepared):
    rig, admission, escrow, owner, _ = prepared
    ref, _ = evidence_pins(admission.evidence)[0]
    rig.run("update-ref", "-d", ref)
    with pytest.raises(ValueError, match="pin"):
        owner.prepare(admission)
    assert not (escrow.root / admission.escrow_dir / "publication").exists()


def test_a_foreign_repo_under_an_owned_path_is_preserved(prepared):
    rig, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    rig.git.run(rig.root, ["worktree", "remove", str(workspace.checkout)])
    workspace.checkout.mkdir()
    rig.git.run(workspace.checkout, ["init"])
    for operation in (owner.prepare, owner.release):
        with pytest.raises(ValueError, match="another repository"):
            operation(admission)
    assert (workspace.checkout / ".git").exists()


def test_verified_capture_rejects_substituted_bytes(prepared):
    _, _, _, _, capture = prepared
    with pytest.raises(ValueError, match="artifact bytes"):
        replace(capture, completion=b"untrusted")


def test_exchange_summary_is_verified_and_materialized_with_this_run(prepared, tmp_path):
    import hashlib
    from issue_orchestrator.domain.validated_work import AdmittedArtifact, ArtifactSlot
    from issue_orchestrator.domain.validated_work_escrow import EscrowArtifacts, escrow_locator

    rig, _, escrow, owner, _ = prepared
    admission, sources = real_capture(rig, tmp_path / "exchange-source")
    summary = b"Recorded reviewer result; no new exchange.\n"
    path = sources.completion.parent / "exchange-summary.md"
    path.write_bytes(summary)
    identity = replace(admission.evidence.identity, exchange_summary_artifact=AdmittedArtifact(
        ArtifactSlot.EXCHANGE_SUMMARY, hashlib.sha256(summary).hexdigest(), len(summary)))
    evidence = replace(admission.evidence, identity=identity)
    pins = evidence_pins(evidence)
    admission = replace(admission, evidence=evidence, escrow_dir=escrow_locator(evidence),
                        pinned_ref=pins[0][0], observed_ref=pins[1][0] if len(pins) == 2 else "")
    escrow.capture(admission, EscrowArtifacts(sources.completion, sources.validation, path))
    workspace = owner.prepare(admission)
    assert workspace.artifacts.exchange_summary.read_bytes() == summary
    owner.release(admission)
    assert escrow.read_capture(admission.escrow_dir).exchange_summary == summary


def test_allocation_crash_before_git_dispatch_replays_from_durable_receipt(prepared):
    from unittest.mock import Mock
    from issue_orchestrator.ports.git import Git

    rig, admission, escrow, _, _ = prepared
    git = Mock(spec=Git, wraps=rig.git)
    def run(repo, argv, **kwargs):
        if argv[:2] == ["worktree", "add"]:
            raise OSError("crash before checkout creation")
        return rig.git.run(repo, argv, **kwargs)
    git.run.side_effect = run
    interrupted = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
                    repo_slug="owner/repo", escrow=escrow, git=git, prepare=lambda _: None)
    with pytest.raises(OSError, match="crash"):
        interrupted.prepare(admission)
    owner = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
                    repo_slug="owner/repo", escrow=escrow, git=rig.git, prepare=lambda _: None)
    workspace = owner.prepare(admission)
    assert rig.git.head_sha(workspace.checkout) == admission.evidence.identity.key.validated_head_sha
    owner.release(admission)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_index_edits_are_never_used_or_removed(prepared, flag):
    rig, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    rig.git.run(workspace.checkout, ["update-index", flag, "file"])
    (workspace.checkout / "file").write_text("operator edit")
    assert not rig.git.status_porcelain(workspace.checkout)
    for operation in (owner.prepare, owner.release):
        with pytest.raises(ValueError, match="index hides"):
            operation(admission)
    assert (workspace.checkout / "file").read_text() == "operator edit"


def test_exact_blob_check_does_not_trust_an_unchanged_stat_cache(prepared):
    from unittest.mock import Mock
    from issue_orchestrator.ports.git import Git, GitResult

    rig, admission, escrow, owner, _ = prepared
    workspace = owner.prepare(admission)
    (workspace.checkout / "file").write_text("operator edit")
    git = Mock(spec=Git, wraps=rig.git)
    def run(repo, argv, **kwargs):
        if argv[0] == "status":
            return GitResult(argv, 0, "", "")
        return rig.git.run(repo, argv, **kwargs)
    git.run.side_effect = run
    checking = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
                    repo_slug="owner/repo", escrow=escrow, git=git, prepare=lambda _: None)
    for operation in (checking.prepare, checking.release):
        with pytest.raises(ValueError, match="content differs"):
            operation(admission)
    assert (workspace.checkout / "file").read_text() == "operator edit"


@pytest.mark.parametrize("boundary", ["publication-owner.json", "completion.json", "validation.json"])
def test_interrupted_atomic_publication_cannot_leave_a_torn_final_file(prepared, monkeypatch, boundary):
    import os

    _, admission, escrow, owner, _ = prepared
    real_link = os.link
    interrupted = False
    def link(source, destination, **kwargs):
        nonlocal interrupted
        if destination.name == boundary and not interrupted:
            interrupted = True
            # A crash while staging can leave arbitrary partial bytes; none
            # become authority at the final name until the atomic link.
            source.write_bytes(b"partial staging")
            raise OSError("interrupted staging")
        return real_link(source, destination, **kwargs)
    monkeypatch.setattr(os, "link", link)
    with pytest.raises(OSError, match="interrupted staging"):
        owner.prepare(admission)
    workspace = owner.prepare(admission)
    assert workspace.artifacts.completion.read_bytes() == escrow.read_capture(admission.escrow_dir).completion
    owner.release(admission)
    assert not workspace.checkout.exists()
    assert any((publication_root(escrow) / ".tmp").iterdir())


def test_atomic_write_never_replaces_an_existing_final_name(tmp_path):
    from issue_orchestrator.infra.escrow_files import publish_durable

    destination = tmp_path / "owned-artifact"
    destination.write_bytes(b"operator contents")
    with pytest.raises(FileExistsError):
        publish_durable(destination, b"replacement", staging_root=tmp_path / ".tmp")
    assert destination.read_bytes() == b"operator contents"


def test_exact_content_check_accepts_binary_executable_and_tracked_symlink(prepared, tmp_path):
    rig, _, escrow, owner, _ = prepared
    executable = rig.root / "executable"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (rig.root / "binary").write_bytes(bytes(range(256)))
    (rig.root / "link").symlink_to("binary")
    rig.run("add", "executable", "binary", "link")
    rig.run("commit", "-m", "test tracked file shapes")
    rig.target = rig.run("rev-parse", "HEAD")
    admission, sources = real_capture(rig, tmp_path / "shapes-source")
    escrow.capture(admission, sources)
    workspace = owner.prepare(admission)
    assert (workspace.checkout / "binary").read_bytes() == bytes(range(256))
    assert (workspace.checkout / "link").is_symlink()
    owner.release(admission)
    escrow.verify_pins(admission)


@pytest.mark.parametrize("boundary", ["registered", "moved", "renamed-before-registration"])
def test_interrupted_git_initialization_preserves_remnants_and_replays(prepared, boundary):
    import os
    from pathlib import Path
    from unittest.mock import Mock
    from issue_orchestrator.ports.git import Git

    rig, admission, escrow, _, _ = prepared
    git = Mock(spec=Git, wraps=rig.git)
    remnant = None
    def run(repo, argv, **kwargs):
        nonlocal remnant
        if boundary == "registered" and argv[:2] == ["worktree", "add"]:
            rig.git.run(repo, [*argv[:2], "--no-checkout", *argv[2:]], **kwargs)
            remnant = Path(argv[-2])
            (remnant / "operator-work").write_text("keep ambiguous initialization")
            raise OSError("interrupted checkout allocation")
        if boundary != "registered" and argv[:2] == ["worktree", "move"]:
            if boundary == "moved":
                rig.git.run(repo, argv, **kwargs)
            else:
                os.rename(argv[-2], argv[-1])
            raise OSError("interrupted checkout move")
        return rig.git.run(repo, argv, **kwargs)
    git.run.side_effect = run
    interrupted = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
                    repo_slug="owner/repo", escrow=escrow, git=git, prepare=lambda _: None)
    with pytest.raises(OSError, match="interrupted checkout"):
        interrupted.prepare(admission)
    owner = EscrowPublicationWorkspaces(root=publication_root(escrow), repository=rig.root,
                    repo_slug="owner/repo", escrow=escrow, git=rig.git, prepare=lambda _: None)
    workspace = owner.prepare(admission)
    assert rig.git.head_sha(workspace.checkout) == rig.target
    assert str(workspace.checkout) in rig.run("worktree", "list", "--porcelain")
    owner.release(admission)
    if remnant is not None:
        assert (remnant / "operator-work").read_text() == "keep ambiguous initialization"
        assert str(remnant) in rig.run("worktree", "list", "--porcelain")


@pytest.mark.parametrize("operation", ["prepare", "release"])
def test_registration_repair_never_changes_an_unrelated_worktree_gitfile(prepared, tmp_path, operation):
    rig, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    unrelated = tmp_path / "unrelated"
    rig.run("worktree", "add", "--detach", str(unrelated), rig.tip)
    gitfile = unrelated / ".git"
    operator_binding = b"gitdir: /operator/selected/directory\n"
    gitfile.write_bytes(operator_binding)
    getattr(owner, operation)(admission)
    assert gitfile.read_bytes() == operator_binding
    if operation == "prepare":
        assert workspace.checkout.exists()


def test_registration_repair_refuses_an_admin_directory_still_bound_elsewhere(prepared, tmp_path):
    from pathlib import Path

    rig, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    admin = Path(rig.git.run(workspace.checkout, ["rev-parse", "--absolute-git-dir"]).stdout.strip())
    other_gitfile = tmp_path / "operator-gitfile"
    other_gitfile.write_text("operator contents")
    backlink = admin / "gitdir"
    binding = f"{other_gitfile}\n"
    backlink.write_text(binding)
    for operation in (owner.prepare, owner.release):
        with pytest.raises(ValueError, match="still bound"):
            operation(admission)
    assert backlink.read_text() == binding
    assert other_gitfile.read_text() == "operator contents"
    assert workspace.checkout.exists()


@pytest.mark.parametrize("operation", ["prepare", "release"])
def test_missing_gitfile_does_not_allow_stealing_a_present_worktrees_admin(prepared, tmp_path, operation):
    from pathlib import Path

    rig, admission, _, owner, _ = prepared
    workspace = owner.prepare(admission)
    unrelated = tmp_path / "unrelated-present"
    rig.run("worktree", "add", "--detach", str(unrelated), rig.target)
    admin = Path(rig.git.run(unrelated, ["rev-parse", "--absolute-git-dir"]).stdout.strip())
    (workspace.checkout / ".git").write_bytes((unrelated / ".git").read_bytes())
    (unrelated / ".git").unlink()
    backlink = admin / "gitdir"
    before = backlink.read_bytes()
    with pytest.raises(ValueError, match="still bound"):
        getattr(owner, operation)(admission)
    assert backlink.read_bytes() == before
    assert (unrelated / "file").read_text() == "1"
    assert workspace.checkout.exists()

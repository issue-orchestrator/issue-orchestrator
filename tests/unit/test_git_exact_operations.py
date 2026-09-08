"""Exact pushes exercise real Git's atomic lease against local bare repositories."""

from pathlib import Path

import pytest

from issue_orchestrator.domain.exact_git import ExactPushOutcome, RefPinOutcome
from issue_orchestrator.domain.validated_work_store import AncestryRelation
from issue_orchestrator.ports.git import GitError
from .git_escrow_support import git_rig


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    return git_rig(tmp_path)


def test_exact_target_ignores_checkout_and_tracking_heads_and_tags(rig):
    rig.run("tag", "-a", "unrelated", "-m", "tag", rig.target)
    rig.run("config", "push.followTags", "true")
    rig.run("update-ref", "refs/remotes/origin/feature", rig.divergent)
    result = rig.working.push_exact(
        rig.root,
        remote="origin",
        branch="feature",
        target_sha=rig.target,
        expected_sha=rig.base,
    )
    assert result.outcome is ExactPushOutcome.PUSHED
    assert rig.remote_refs() == f"{rig.target} refs/heads/feature\n"
    assert rig.run("rev-parse", "HEAD") == rig.tip


def test_absent_branch_has_empty_atomic_expectation(rig):
    args = dict(remote="origin", branch="new", target_sha=rig.target, expected_sha=None)
    assert rig.working.push_exact(rig.root, **args).outcome is ExactPushOutcome.PUSHED
    before = rig.remote_refs()
    args["target_sha"] = rig.tip
    assert (
        rig.working.push_exact(rig.root, **args).outcome
        is ExactPushOutcome.LEASE_REJECTED
    )
    assert rig.remote_refs() == before


def test_concurrent_remote_move_never_becomes_a_new_lease(rig):
    rig.run("push", "origin", f"{rig.tip}:refs/heads/feature")
    before = rig.remote_refs()
    result = rig.working.push_exact(
        rig.root,
        remote="origin",
        branch="feature",
        target_sha=rig.target,
        expected_sha=rig.base,
    )
    assert result.outcome is ExactPushOutcome.LEASE_REJECTED
    assert rig.remote_refs() == before


@pytest.mark.parametrize("target,expected", [("divergent", "base"), ("base", "target")])
def test_divergence_and_rewind_do_not_write(rig, target, expected):
    # divergence fixture is descended from base; target -> divergence is divergent.
    if target == "divergent":
        expected = "target"
    rig.run("push", "origin", f"{getattr(rig, expected)}:refs/heads/feature")
    before = rig.remote_refs()
    result = rig.working.push_exact(
        rig.root,
        remote="origin",
        branch="feature",
        target_sha=getattr(rig, target),
        expected_sha=getattr(rig, expected),
    )
    assert result.outcome is ExactPushOutcome.NOT_FAST_FORWARD
    assert rig.remote_refs() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("branch", "--all"),
        ("branch", "x:y"),
        ("branch", "refs/heads/x"),
        ("branch", "x\ny"),
        ("remote", "--mirror"),
        ("remote", "/tmp/other"),
        ("remote", "ext::evil"),
        ("target_sha", "HEAD"),
        ("expected_sha", "abc"),
        ("target_sha", "A" * 40),
    ],
)
def test_invalid_inputs_never_write(rig, field, value):
    args = dict(
        remote="origin", branch="feature", target_sha=rig.target, expected_sha=rig.base
    )
    args[field] = value
    before = rig.remote_refs()
    with pytest.raises((ValueError, GitError)):
        rig.working.push_exact(rig.root, **args)
    assert rig.remote_refs() == before


def test_pins_survive_worktree_removal_and_gc_without_head_substitution(rig, tmp_path):
    worktree = tmp_path / "disposable"
    rig.run("worktree", "add", "--detach", str(worktree), rig.target)
    ref = "refs/issue-orchestrator/validated/1/e1-" + "a" * 64
    assert (
        rig.working.pin_ref(worktree, ref=ref, sha=rig.target) is RefPinOutcome.PINNED
    )
    assert rig.working.pin_ref(worktree, ref=ref, sha=rig.tip) is RefPinOutcome.CONFLICT
    assert (
        rig.working.pin_ref(worktree, ref=ref + "missing", sha="f" * 40)
        is RefPinOutcome.OBJECT_MISSING
    )
    rig.run("worktree", "remove", str(worktree))
    rig.run("gc", "--prune=now")
    assert rig.working.verify_ref(rig.root, ref=ref, sha=rig.target)
    assert (
        rig.working.compare_commits(rig.root, left=rig.target, right=rig.tip)
        is AncestryRelation.ANCESTOR
    )


def test_symbolic_pin_is_never_followed_or_overwritten(rig):
    ref = "refs/issue-orchestrator/observed/1/e1-" + "b" * 64
    rig.run("symbolic-ref", ref, "refs/heads/main")
    with pytest.raises(ValueError, match="symbolic"):
        rig.working.pin_ref(rig.root, ref=ref, sha=rig.target)
    assert rig.run("rev-parse", "main") == rig.tip


def test_lease_rejects_move_between_precheck_and_write(rig):
    from issue_orchestrator.adapters.git.git_cli import GitCLI
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

    # Supply the interfering commit to the bare repo without changing feature.
    rig.run("push", "origin", f"{rig.tip}:refs/heads/interference-object")

    class MovingGit(GitCLI):
        def run(self, repo, argv, **kwargs):
            if "push" in argv:
                rig.git.run(
                    rig.remote, ["update-ref", "refs/heads/feature", rig.tip, rig.base]
                )
            if "fetch" in argv:
                pytest.fail("exact publication must not fetch")
            return super().run(repo, argv, **kwargs)

    working = GitWorkingCopy(git=MovingGit(runner=rig.git.runner))
    result = working.push_exact(
        rig.root,
        remote="origin",
        branch="feature",
        target_sha=rig.target,
        expected_sha=rig.base,
    )
    assert result.outcome is ExactPushOutcome.LEASE_REJECTED
    assert (
        rig.remote_refs()
        == f"{rig.tip} refs/heads/feature\n{rig.tip} refs/heads/interference-object\n"
    )


@pytest.mark.parametrize(
    "left,right,relation",
    [
        ("base", "target", AncestryRelation.ANCESTOR),
        ("tip", "target", AncestryRelation.DESCENDANT),
        ("divergent", "target", AncestryRelation.DIVERGENT),
        ("target", "target", AncestryRelation.EQUAL),
        ("missing", "target", AncestryRelation.LEFT_UNREACHABLE),
        ("target", "missing", AncestryRelation.RIGHT_UNREACHABLE),
        ("missing", "missing", AncestryRelation.BOTH_UNREACHABLE),
    ],
)
def test_typed_ancestry(rig, left, right, relation):
    sha = lambda key: "f" * 40 if key == "missing" else getattr(rig, key)
    assert (
        rig.working.compare_commits(rig.root, left=sha(left), right=sha(right))
        is relation
    )


def test_missing_expectation_object_cannot_authorize_write(rig):
    before = rig.remote_refs()
    result = rig.working.push_exact(
        rig.root,
        remote="origin",
        branch="feature",
        target_sha=rig.target,
        expected_sha="f" * 40,
    )
    assert result.outcome is ExactPushOutcome.NOT_FAST_FORWARD
    assert rig.remote_refs() == before


def test_git_replace_objects_cannot_forge_fast_forward(rig):
    # Make target appear to have divergent as a parent through a replacement commit.
    tree = rig.run("rev-parse", f"{rig.target}^{{tree}}")
    forged = rig.run("commit-tree", tree, "-p", rig.divergent, "-m", "forged history")
    rig.run("replace", rig.target, forged)
    assert (
        rig.working.compare_commits(rig.root, left=rig.divergent, right=rig.target)
        is AncestryRelation.DIVERGENT
    )


@pytest.mark.parametrize("config_key", ["pushurl", "url"])
def test_multiple_remote_endpoints_are_rejected_before_any_write(
    rig, tmp_path, config_key
):
    other = tmp_path / "second.git"
    other.mkdir()
    rig.git.run(other, ["init", "--bare"])
    rig.run(
        "config",
        "--add",
        f"remote.origin.{config_key}",
        str(rig.remote) if config_key == "pushurl" else str(other),
    )
    if config_key == "pushurl":
        rig.run("config", "--add", "remote.origin.pushurl", str(other))
    before = rig.remote_refs()
    with pytest.raises(ValueError, match="one configured push endpoint"):
        rig.working.push_exact(
            rig.root,
            remote="origin",
            branch="feature",
            target_sha=rig.target,
            expected_sha=rig.base,
        )
    assert rig.remote_refs() == before
    assert rig.git.run(other, ["for-each-ref"]).stdout == ""


def test_remote_configuration_change_cannot_add_a_write_endpoint(rig, tmp_path):
    from issue_orchestrator.adapters.git.git_cli import GitCLI
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

    other = tmp_path / "second.git"
    other.mkdir()
    rig.git.run(other, ["init", "--bare"])

    class ConfigChangingGit(GitCLI):
        def run(self, repo, argv, **kwargs):
            if "push" in argv:
                rig.run("config", "--add", "remote.origin.pushurl", str(rig.remote))
                rig.run("config", "--add", "remote.origin.pushurl", str(other))
            return super().run(repo, argv, **kwargs)

    result = GitWorkingCopy(git=ConfigChangingGit(runner=rig.git.runner)).push_exact(
        rig.root,
        remote="origin",
        branch="feature",
        target_sha=rig.target,
        expected_sha=rig.base,
    )
    assert result.outcome is ExactPushOutcome.PUSHED
    assert rig.remote_refs() == f"{rig.target} refs/heads/feature\n"
    assert rig.git.run(other, ["for-each-ref"]).stdout == ""


def test_grafts_cannot_forge_fast_forward(rig):
    # Commit objects remain divergent even when local graft metadata lies.
    rig.run("push", "origin", f"{rig.divergent}:refs/heads/feature")
    before = rig.remote_refs()
    (rig.root / ".git/info/grafts").write_text(f"{rig.target} {rig.divergent}\n")
    assert (
        rig.working.compare_commits(rig.root, left=rig.divergent, right=rig.target)
        is AncestryRelation.DIVERGENT
    )
    with pytest.raises(ValueError, match="graft"):
        rig.working.push_exact(
            rig.root, remote="origin", branch="feature",
            target_sha=rig.target, expected_sha=rig.divergent,
        )
    assert rig.remote_refs() == before


def test_shallow_metadata_does_not_change_exact_commit_ancestry(rig):
    (rig.root / ".git/shallow").write_text(f"{rig.target}\n")
    assert (
        rig.working.compare_commits(rig.root, left=rig.base, right=rig.target)
        is AncestryRelation.ANCESTOR
    )
    assert (
        rig.working.compare_commits(rig.root, left=rig.divergent, right=rig.target)
        is AncestryRelation.DIVERGENT
    )


def test_recursive_submodule_configuration_cannot_add_writes(rig, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    subspace = tmp_path / "sub"
    subspace.mkdir()
    subrig = git_rig(subspace)
    subrig.git.run(subrig.remote, ["symbolic-ref", "HEAD", "refs/heads/feature"])
    rig.run("config", "protocol.file.allow", "always")
    rig.run(
        "submodule",
        "add",
        str(subrig.remote),
        "module",
    )
    module = rig.root / "module"
    rig.git.run(module, ["config", "user.name", "Escrow Test"])
    rig.git.run(module, ["config", "user.email", "escrow@example.invalid"])
    (module / "new").write_text("unpushed submodule commit")
    rig.git.run(module, ["add", "new"])
    rig.git.run(module, ["commit", "-m", "new submodule"])
    rig.run("add", ".gitmodules", "module")
    rig.run("commit", "-m", "submodule parent")
    target = rig.run("rev-parse", "HEAD")
    rig.run("config", "push.recurseSubmodules", "on-demand")
    before = subrig.remote_refs()
    result = rig.working.push_exact(
        rig.root,
        remote="origin",
        branch="feature",
        target_sha=target,
        expected_sha=rig.base,
    )
    assert result.outcome is ExactPushOutcome.PUSHED
    assert rig.remote_refs() == f"{target} refs/heads/feature\n"
    assert subrig.remote_refs() == before

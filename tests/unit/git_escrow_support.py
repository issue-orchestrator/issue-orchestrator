"""Real local-only Git fixtures for exact writes and durable capture."""

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from issue_orchestrator.adapters.git.git_cli import GitCLI
from issue_orchestrator.domain.validated_work import AdmittedArtifact, ArtifactSlot
from issue_orchestrator.domain.validated_work_escrow import (
    EscrowArtifacts,
    escrow_locator,
    evidence_pins,
)
from issue_orchestrator.domain.validated_work_store import EvidenceAdmission
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from .validated_work_support import capture


@dataclass
class GitRig:
    root: Path
    remote: Path
    git: GitCLI
    working: GitWorkingCopy
    base: str
    target: str
    tip: str
    divergent: str

    def run(self, *args: str) -> str:
        return self.git.run(self.root, list(args)).stdout.strip()

    def remote_refs(self) -> str:
        return self.git.run(self.remote, ["show-ref"]).stdout


def git_rig(tmp_path: Path) -> GitRig:
    git = GitCLI(runner=LocalCommandRunner())
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    repo.mkdir()
    remote.mkdir()
    git.run(repo, ["init", "-b", "main"])
    git.run(remote, ["init", "--bare"])
    git.run(repo, ["config", "user.name", "Escrow Test"])
    git.run(repo, ["config", "user.email", "escrow@example.invalid"])
    git.run(repo, ["remote", "add", "origin", str(remote)])
    heads = []
    for n in range(3):
        (repo / "file").write_text(str(n))
        git.run(repo, ["add", "file"])
        git.run(repo, ["commit", "-m", f"commit {n}"])
        heads.append(git.run(repo, ["rev-parse", "HEAD"]).stdout.strip())
    git.run(repo, ["checkout", "--detach", heads[0]])
    (repo / "other").write_text("divergent")
    git.run(repo, ["add", "other"])
    git.run(repo, ["commit", "-m", "divergence"])
    divergent = git.run(repo, ["rev-parse", "HEAD"]).stdout.strip()
    git.run(repo, ["checkout", "main"])
    git.run(repo, ["push", "origin", f"{heads[0]}:refs/heads/feature"])
    return GitRig(repo, remote, git, GitWorkingCopy(git=git), *heads, divergent)


def real_capture(
    rig: GitRig, source: Path, **kwargs
) -> tuple[EvidenceAdmission, EscrowArtifacts]:
    admission = capture(rig.target, expected=rig.base, **kwargs)
    source.mkdir(parents=True, exist_ok=True)
    sources = EscrowArtifacts(source / "completion", source / "validation", None)
    artifacts = []
    for slot, path, data in (
        (ArtifactSlot.COMPLETION, sources.completion, b'{"complete":true}\n'),
        (ArtifactSlot.VALIDATION, sources.validation, b'{"valid":true}\n'),
    ):
        path.write_bytes(data)
        artifacts.append(
            AdmittedArtifact(slot, hashlib.sha256(data).hexdigest(), len(data))
        )
    identity = replace(
        admission.evidence.identity,
        completion_artifact=artifacts[0],
        validation_artifact=artifacts[1],
    )
    evidence = replace(admission.evidence, identity=identity)
    pins = evidence_pins(evidence)
    admission = replace(
        admission,
        evidence=evidence,
        escrow_dir=escrow_locator(evidence),
        pinned_ref=pins[0][0],
        observed_ref=pins[1][0] if len(pins) == 2 else "",
    )
    return admission, sources

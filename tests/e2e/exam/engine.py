"""The engine under examination: a checkout of some commit, run for one case.

Every exam run gets a FRESH standalone clone of the commit under test,
because the engine keeps its state under ``<repo_root>/.issue-orchestrator``:
running two commits out of one checkout would hand the older engine the newer
one's databases, and re-running one commit would hand a case the previous
case's run history. The checkout reuses the harness's virtualenv (symlinked),
and ``OrchestratorProcess`` puts the checkout's ``src`` first on PYTHONPATH,
so the engine — and every completion command its agents run — is the commit
under test, while the harness, its shims and its grader stay this tree's.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config

from tests.e2e.exam.agents import CODER_LABEL, REVIEWER_LABEL, TECH_LEAD_LABEL, shim_command
from tests.e2e.fixtures import OrchestratorProcess, find_free_port
from tests.e2e.fixtures.orchestrator_process import merge_config_overlay
from tests.e2e.fixtures.inflight_tracker import control_api_headers
from tests.e2e.flows import OrchestratorRuntime, start_orchestrator_runtime

logger = logging.getLogger(__name__)

WORKTREE_PARENT = Path.home() / "dev" / "worktree" / "issue-orchestrator"

TECH_LEAD_PROMPT = Path("repo-specific") / "prompts" / "tech-lead.md"

#: Settings every exam engine runs with, under each case's own overlay.
#: Session interactions answer the startup screens real agents open on — the
#: rules #7299 fixed (Claude Code's "Quick safety check" with "No, exit"
#: highlighted, Codex's "Folder access"). They are off by default; with them
#: off, the exam's first real tech lead sat 35 minutes on the trust dialog of
#: its fresh /tmp worktree with no event and no log line.
EXAM_BASE_OVERLAY: Mapping[str, Any] = {
    "execution": {"session_interactions": {"enabled": True}},
}


@dataclass(frozen=True)
class EngineCheckout:
    """A fresh, standalone checkout of the commit under test.

    A standalone ``--shared`` clone, not a ``git worktree``: the engine
    refuses to run with its repository inside a linked worktree (the
    validated-work escrow must live outside disposable worktrees). Objects
    are borrowed from the harness's repository, so the clone is cheap; its
    ``origin`` is re-pointed at GitHub so the engine pushes where a real
    engine would.
    """

    root: Path
    commit: str

    @classmethod
    def create(cls, *, harness_root: Path, ref: str, case_id: str) -> "EngineCheckout":
        commit = _git(harness_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
        origin = _git(harness_root, "remote", "get-url", "origin")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root = WORKTREE_PARENT / f"exam-engine-{commit[:10]}-{case_id[:1].lower()}-{stamp}"
        venv = harness_root / ".venv"
        if not venv.is_dir():
            raise RuntimeError(f"harness virtualenv missing at {venv}")
        _git(harness_root, "clone", "--quiet", "--shared", "--no-checkout", str(harness_root), str(root))
        checkout = cls(root=root, commit=commit)
        try:
            _git(root, "remote", "set-url", "origin", origin)
            _git(root, "fetch", "--quiet", "origin", "main")
            # Agent worktrees branch from local ``main`` (ORCHESTRATOR_WORKTREE_BASE_BRANCH).
            _git(root, "branch", "--force", "main", "origin/main")
            _git(root, "checkout", "--quiet", "--detach", commit)
            (root / ".venv").symlink_to(venv)
        except BaseException:
            # A half-built clone is nobody's to clean up later (a network
            # outage mid-fetch left one behind).
            checkout.remove()
            raise
        logger.info("[EXAM] engine checkout %s at %s", root, commit)
        return checkout

    @property
    def state_dir(self) -> Path:
        return self.root / ".issue-orchestrator" / "state"

    def remove(self) -> None:
        link = self.root / ".venv"
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(self.root)


def _git(cwd: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(argv)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def exam_config(
    base: Config,
    *,
    checkout: EngineCheckout,
    run_label: str,
    reviewer_exchange_fault: str,
    tech_lead_model: str | None = None,
) -> Config:
    """The e2e session config, pointed at the checkout, with exam agents."""
    config = copy.deepcopy(base)
    config.repo_root = checkout.root
    config.control_api_port = find_free_port()
    config.web_port = find_free_port()
    config.filtering.label = run_label
    config.e2e_pr_labels = [run_label, "io-e2e-test-data"]
    config.max_concurrent_sessions = 2
    config.queue_refresh_seconds = 30
    config.session_timeout_minutes = 10
    config.code_review_agent = REVIEWER_LABEL
    config.tech_lead_review_agent = TECH_LEAD_LABEL if tech_lead_model else None
    prompt = checkout.root / "tests" / "e2e" / "fixtures" / "prompts" / "simple_task.md"
    config.agents = {
        CODER_LABEL: AgentConfig(
            prompt_path=prompt,
            timeout_minutes=3,
            model="sonnet",
            command=shim_command("coder"),
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
            reviewer=REVIEWER_LABEL,
        ),
        REVIEWER_LABEL: AgentConfig(
            prompt_path=prompt,
            timeout_minutes=1,
            model="sonnet",
            command=shim_command("reviewer", exchange_fault=reviewer_exchange_fault),
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        ),
    }
    if tech_lead_model:
        config.agents[TECH_LEAD_LABEL] = AgentConfig(
            prompt_path=checkout.root / TECH_LEAD_PROMPT,
            timeout_minutes=45,
            model=tech_lead_model,
            provider="claude-code",
            ai_system="claude-code",
            # Production's tech-lead agent (the operator's io/porchpin configs).
            provider_args={"effort": "xhigh", "permission_mode": "bypassPermissions"},
            initial_prompt=(
                "Tech Lead review for issue #{issue_number}: {issue_title}. Follow the"
                " instructions in {prompt}. When done, use coding-done to report completion."
            ),
        )
    return config


class ExamEngine:
    """One running engine plus its control surfaces."""

    def __init__(
        self,
        config: Config,
        checkout: EngineCheckout,
        *,
        overlay: Mapping[str, Any],
    ) -> None:
        self.config = config
        self.checkout = checkout
        self.process = OrchestratorProcess(
            config,
            checkout.root,
            source_root=checkout.root,
            config_overlay=merge_config_overlay(EXAM_BASE_OVERLAY, overlay),
        )
        self._runtime: OrchestratorRuntime | None = None

    @property
    def runtime(self) -> OrchestratorRuntime:
        if self._runtime is None:
            raise RuntimeError("engine not started")
        return self._runtime

    async def start(self) -> OrchestratorRuntime:
        self._runtime = await start_orchestrator_runtime(
            self.process,
            self.config.control_api_port,
            max_issues=10,
            extra_args=["--label", self.config.filtering.label],
        )
        # Keep every event for the report, not the watcher's default 200.
        self._runtime.watcher.view.set_diag_limits(100_000)
        return self._runtime

    async def close(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()
        elif self.process.is_running():
            self.process.stop()

    def is_running(self) -> bool:
        return self.process.is_running()

    def active_session_issues(self) -> tuple[tuple[str, int], ...]:
        """``(session_name, issue_number)`` of every session the engine runs now."""
        status = self._get_json(self.config.control_api_port, "/api/status")
        sessions = status.get("sessions")
        if not isinstance(sessions, list):
            raise RuntimeError(f"/api/status has no sessions list: {status!r}")
        return tuple(
            (str(entry["session_name"]), int(entry["issue_number"])) for entry in sessions
        )

    def active_sessions(self) -> int:
        status = self._get_json(self.config.control_api_port, "/api/status")
        value = status.get("active_sessions")
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"/api/status active_sessions is not an int: {status!r}")
        return value

    def _get_json(self, port: int, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"http://localhost:{port}{path}", headers=control_api_headers()
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

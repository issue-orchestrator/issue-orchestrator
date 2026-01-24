"""HookVerifier adapter - verifies AI agent hooks on startup."""

import logging

from ..ports.hook_verifier import HookVerificationResult
from ..infra.config import Config

logger = logging.getLogger(__name__)


class ExecutionHookVerifier:
    """Execution-layer hook verification using local hook checks."""

    def __init__(self, config: Config):
        self.config = config

    def _verify_single_agent(
        self,
        agent_type,
        get_adapter,
        UnsupportedAiAgentError,
    ) -> tuple[bool, tuple[str, str] | None]:
        """Verify hooks for a single agent type.

        Returns:
            Tuple of (success, unsupported_info).
            unsupported_info is (agent_type_value, reason) if unsupported, else None.
        """
        try:
            adapter = get_adapter(agent_type)

            if not adapter.is_installed(self.config.repo_root):
                logger.error(
                    "Hooks not installed for %s. Run 'issue-orchestrator setup-hooks'",
                    agent_type.value,
                )
                print(f"[ERROR] Hooks not installed for {agent_type.value}")
                print("        Run 'issue-orchestrator setup-hooks' to install them")
                return False, None

            result = adapter.verify_hooks(self.config.repo_root)
            if result.success:
                logger.info(
                    "Hooks verified for %s (%d checks)",
                    agent_type.value,
                    len(result.checks_passed),
                )
                print(f"[OK] Hooks verified for {agent_type.value}")
                return True, None

            logger.error(
                "Hook verification failed for %s: %s",
                agent_type.value,
                result.checks_failed,
            )
            print(f"[ERROR] Hook verification failed for {agent_type.value}")
            for failure in result.checks_failed:
                print(f"        - {failure}")
            return False, None

        except UnsupportedAiAgentError as exc:
            return True, (agent_type.value, str(exc))

    def _handle_unsupported_agents(
        self,
        unsupported: list[tuple[str, str]],
    ) -> bool:
        """Handle unsupported agents, return True if all allowed."""
        if not unsupported:
            return True

        if self.config.dangerous.allow_unsupported_agents:
            for agent_type_val, reason in unsupported:
                logger.warning(
                    "[DANGEROUS] Allowing unsupported agent %s: %s",
                    agent_type_val,
                    reason,
                )
                print(f"[WARNING] Unsupported agent {agent_type_val} allowed (dangerous mode)")
            return True

        for agent_type_val, reason in unsupported:
            print(f"[ERROR] Unsupported AI agent: {agent_type_val}")
            print(f"        {reason}")
        print("\nTo allow unsupported agents, set dangerous.allow_unsupported_agents: true")
        return False

    async def verify(self) -> HookVerificationResult:
        from ..infra.hooks.hooks import (
            detect_agents_from_config,
            get_adapter,
            check_verification_status,
            UnsupportedAiAgentError,
        )

        is_valid, status_msg = check_verification_status(self.config.repo_root, self.config)
        if is_valid:
            logger.info("Using cached verification: %s", status_msg)
            print(f"[OK] Hooks verified (cached): {status_msg}")
            return HookVerificationResult(success=True, message=f"cached: {status_msg}")

        logger.info("No valid verification marker found - running full verification")

        agent_types = detect_agents_from_config(self.config)
        unique_types = set(agent_types.values())
        logger.info("Verifying hooks for AI agents: %s", [t.value for t in unique_types])

        all_verified = True
        unsupported: list[tuple[str, str]] = []

        for agent_type in unique_types:
            success, unsupported_info = self._verify_single_agent(
                agent_type, get_adapter, UnsupportedAiAgentError
            )
            if not success:
                all_verified = False
            if unsupported_info:
                unsupported.append(unsupported_info)
                if not self.config.dangerous.allow_unsupported_agents:
                    logger.error("Unsupported AI agent: %s", unsupported_info[1])
                    all_verified = False

        if not self._handle_unsupported_agents(unsupported):
            all_verified = False

        if all_verified:
            return HookVerificationResult(success=True, message="verified")

        return HookVerificationResult(
            success=False,
            message="verification failed",
            unsupported_agents=unsupported,
        )

    def raise_on_failure(self, result: HookVerificationResult) -> None:
        if not result.success:
            print("\n" + "=" * 60)
            print("STARTUP BLOCKED: Hook verification failed")
            print("=" * 60)
            print("\nWithout verified hooks, agents can bypass --no-verify")
            print("and push code without running pre-push tests/checks.")
            print("\nOptions:")
            print("  1. Run 'issue-orchestrator setup-hooks' to install hooks")
            print("  2. Run 'issue-orchestrator verify' to diagnose issues")
            print()
            raise RuntimeError("Hook verification failed - cannot start orchestrator safely")

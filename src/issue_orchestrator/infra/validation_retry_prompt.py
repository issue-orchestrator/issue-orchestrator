"""Render the prompt that sends a validation failure back to its agent."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .validation_state import DEFAULT_RETRY_TEMPLATE, _truncate_with_tail

if TYPE_CHECKING:
    from ..domain.models import PendingValidationRetry
    from .config import Config

logger = logging.getLogger(__name__)


def render_validation_retry_prompt(
    *,
    retry: "PendingValidationRetry",
    issue_number: int,
    issue_title: str,
    agent_template: str | None,
    config: "Config",
    retry_count: int,
) -> str:
    """Render the prompt that sends a validation failure back to its agent.

    The retry template's owner renders it: an agent's own
    ``retry_prompt_template`` wins over the configured one, which wins over
    :data:`DEFAULT_RETRY_TEMPLATE`. A prompt that already IS a retry prompt is
    reused verbatim rather than wrapped again.
    """
    if retry.original_prompt and retry.original_prompt.lstrip().startswith("# Validation Retry"):
        return retry.original_prompt
    validation_cmd = retry.validation_cmd or config.validation.quick.cmd or ""
    original_task = retry.original_prompt or f"Work on issue #{issue_number}: {issue_title}"
    template = DEFAULT_RETRY_TEMPLATE
    template_path = agent_template or config.retry.retry_prompt_template
    if template_path:
        full_template_path = config.repo_root / template_path
        if full_template_path.exists():
            try:
                template = full_template_path.read_text()
            except OSError as exc:
                logger.warning("Failed to load retry template from %s: %s", full_template_path, exc)
        else:
            logger.warning("Retry template not found at %s, using default", full_template_path)
    display_count = retry_count + 1
    display_max = config.retry.max_validation_retries + 1
    return template.format(
        original_task=original_task,
        validation_cmd=validation_cmd,
        error_file=retry.validation_error_file or "unknown",
        error_summary=_truncate_with_tail(retry.validation_error or "Unknown validation error"),
        retry_count=display_count,
        max_retries=display_max,
        retries_remaining=max(0, display_max - display_count),
    )

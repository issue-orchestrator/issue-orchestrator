"""Shared transition logging helpers."""

import logging

logger = logging.getLogger(__name__)


def log_transition(
    entity_type: str,
    number: int,
    from_state: str,
    to_state: str,
    reason: str,
    extra: dict[str, object] | None = None,
) -> None:
    """Log a state transition in a consistent, searchable format."""
    logger.info(
        "[TRANSITION] %s #%d: %s → %s (%s)",
        entity_type,
        number,
        from_state,
        to_state,
        reason,
    )
    if extra:
        logger.debug("[TRANSITION] #%d extra: %s", number, extra)

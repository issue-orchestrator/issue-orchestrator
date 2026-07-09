"""Actionable dependency gate error messages."""

from __future__ import annotations

from ..domain.dependencies import DependencyMode, ParsedDependencyRef


def source_missing_milestone_error(
    issue_number: int,
    ref: ParsedDependencyRef,
    foundation_milestone: str,
) -> str:
    target = _dependency_ref_display(ref)
    directive = "Stack-after" if ref.mode == DependencyMode.STACK else "Depends-on"
    return (
        f"Issue #{issue_number} has no milestone, so {directive}: {target} "
        "cannot be evaluated against milestone-scoped dependencies. "
        f"Fix: assign issue #{issue_number} and {target} to the same milestone, "
        f"or use foundation milestone {foundation_milestone} for intentionally "
        "shared prerequisites."
    )


def milestone_scope_error(
    dep_milestone: str | None,
    source_milestone: str,
    foundation_milestone: str,
) -> str | None:
    if dep_milestone is None:
        return (
            "Dependency has no milestone while the source issue is in "
            f"{source_milestone}. Fix: assign the dependency to "
            f"{source_milestone}, or to foundation milestone "
            f"{foundation_milestone} if it is intentionally shared."
        )
    if dep_milestone != source_milestone and dep_milestone != foundation_milestone:
        return (
            f"Dependency is in {dep_milestone}, but the source issue is in "
            f"{source_milestone}. Fix: move both issues to the same milestone, "
            f"or move the dependency to foundation milestone {foundation_milestone} "
            "if it is intentionally shared."
        )
    return None


def _dependency_ref_display(ref: ParsedDependencyRef) -> str:
    if ref.issue_number is not None:
        if ref.repository:
            return f"{ref.repository}#{ref.issue_number}"
        return f"#{ref.issue_number}"
    if ref.external_id:
        return ref.external_id
    return (ref.source_text or "dependency").strip()

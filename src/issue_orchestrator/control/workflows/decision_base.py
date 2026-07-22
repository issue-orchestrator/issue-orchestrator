"""Base class for workflow decisions.

All workflow decisions (Review, Rework, Tech Lead) share a common pattern:
- should_launch: bool
- items_to_launch: tuple of items
- skip_reason: Optional[str]
- available_capacity: int
- skip() / launch() factory methods

This module provides a generic base class that eliminates ~100 lines of
duplicate code across the three decision classes.

Usage:
    @dataclass(frozen=True)
    class ReviewDecision(WorkflowDecision[PendingReview]):
        pass

    # Can also add item-specific properties via property alias:
    @property
    def reviews_to_launch(self) -> tuple[PendingReview, ...]:
        return self.items_to_launch
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, Optional, Sequence, TypeVar

if TYPE_CHECKING:
    from typing import Self

# Type variable for the item type (PendingReview, PendingRework, etc.)
T = TypeVar("T")


@dataclass(frozen=True)
class WorkflowDecision(Generic[T]):
    """Generic base class for workflow decisions.

    This is the output of a workflow's decision logic.
    It describes WHAT should happen, not HOW.

    Type Parameters:
        T: The type of item to be launched (e.g., PendingReview, PendingRework)
    """

    should_launch: bool = False
    items_to_launch: tuple[T, ...] = field(default_factory=tuple)
    skip_reason: Optional[str] = None
    available_capacity: int = 0

    @classmethod
    def skip(cls, reason: str) -> "Self":
        """Create a decision to skip processing.

        Args:
            reason: Why processing is being skipped

        Returns:
            A decision with should_launch=False and the skip reason set
        """
        return cls(should_launch=False, skip_reason=reason)

    @classmethod
    def launch(
        cls,
        items: Sequence[T],
        capacity: int,
    ) -> "Self":
        """Create a decision to launch items.

        Args:
            items: The items to launch
            capacity: Available capacity at decision time

        Returns:
            A decision with should_launch=True and items set
        """
        return cls(
            should_launch=True,
            items_to_launch=tuple(items),
            available_capacity=capacity,
        )

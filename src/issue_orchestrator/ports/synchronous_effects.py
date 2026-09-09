"""Scope every synchronous boundary operation under its caller's authority."""

from collections.abc import Callable
from typing import Protocol, TypeVar

T = TypeVar("T")


class SynchronousEffectScope(Protocol):
    def perform(self, effect: Callable[[], T]) -> T: ...

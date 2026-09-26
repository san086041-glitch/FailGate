from __future__ import annotations

from collections.abc import Hashable, Iterable, Iterator
from typing import TypeVar

T = TypeVar("T", bound=Hashable)


def unique(items: Iterable[T]) -> list[T]:
    """Drop duplicates, keeping the first occurrence of each item in order."""
    return list(dict.fromkeys(items))


def chunks(items: list[T], size: int) -> Iterator[list[T]]:
    if size <= 0:
        raise ValueError("size must be positive")
    for i in range(0, len(items), size):
        yield items[i : i + size]

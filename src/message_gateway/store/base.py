"""The `Store` protocol: A1-notation reads and writes over named tabs."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    def read(self, range_: str) -> list[list[str]]:
        """Return the rows in `range_` (e.g. 'Tasks!A2:U'), ragged as stored."""
        ...

    def append(self, range_: str, row: list[str]) -> None:
        """Append one row to the tab named in `range_`."""
        ...

    def update(self, range_: str, values: list[list[str]]) -> None:
        """Overwrite the cells starting at `range_` (which must name a row)."""
        ...

    def batch_update(self, updates: list[tuple[str, list[list[str]]]]) -> None:
        """Apply several `update`s in one round trip."""
        ...

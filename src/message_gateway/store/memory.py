"""In-memory `Store`. Each tab is a list of rows with the header at index 0,
so row numbers match the spreadsheet (row 1 = header)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..schema import ALL_TABS, TabSchema

_RANGE_RE = re.compile(r"^([A-Za-z_]+)!([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?$")


def col_index(letter: str) -> int:
    """'A' -> 0, 'Z' -> 25, 'AA' -> 26."""
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


class InMemoryStore:
    def __init__(self) -> None:
        self.tabs: dict[str, list[list[str]]] = {}

    @classmethod
    def with_schema(cls, tabs: Iterable[TabSchema] = ALL_TABS) -> InMemoryStore:
        """A store whose tabs exist with header rows taken from the schema."""
        store = cls()
        for tab in tabs:
            store.tabs[tab.name] = [tab.header_row]
        return store

    def rows(self, tab: TabSchema | str) -> list[list[str]]:
        """Data rows of a tab (header excluded)."""
        name = tab if isinstance(tab, str) else tab.name
        return self.tabs.get(name, [])[1:]

    def _parse(self, range_: str) -> tuple[str, int, int, int | None, int | None]:
        m = _RANGE_RE.match(range_)
        if not m:
            raise ValueError(f"InMemoryStore cannot parse range: {range_}")
        tab, c1, r1, c2, r2 = m.groups()
        return (
            tab,
            col_index(c1),
            col_index(c2 or c1),
            int(r1) if r1 else None,
            int(r2) if r2 else None,
        )

    def read(self, range_: str) -> list[list[str]]:
        tab, col_a, col_b, row_a, row_b = self._parse(range_)
        rows = self.tabs.get(tab, [])
        start_idx = (row_a - 1) if row_a else 0
        end_idx = row_b if row_b else len(rows)
        return [r[col_a : col_b + 1] for r in rows[start_idx:end_idx]]

    def append(self, range_: str, row: list[str]) -> None:
        tab = self._parse(range_)[0]
        self.tabs.setdefault(tab, []).append(list(row))

    def update(self, range_: str, values: list[list[str]]) -> None:
        tab, col_a, _col_b, row_a, _row_b = self._parse(range_)
        rows = self.tabs.setdefault(tab, [])
        if not row_a:
            raise ValueError(f"update needs an explicit row: {range_}")
        for vi, value_row in enumerate(values):
            target_row_idx = (row_a - 1) + vi
            while len(rows) <= target_row_idx:
                rows.append([])
            tgt = rows[target_row_idx]
            for ci, val in enumerate(value_row):
                col = col_a + ci
                while len(tgt) <= col:
                    tgt.append("")
                tgt[col] = str(val)

    def batch_update(self, updates: list[tuple[str, list[list[str]]]]) -> None:
        for rng, vals in updates:
            self.update(rng, vals)

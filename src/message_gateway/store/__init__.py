"""Tabular storage behind the core: a `Store` protocol and two implementations.

- `InMemoryStore` keeps tabs as lists of rows (used by the tests).
- `GoogleSheetsStore` talks to the Sheets API (imported from
  `message_gateway.store.sheets` so the API client stays optional at import).
"""

from .base import Store
from .memory import InMemoryStore

__all__ = ["InMemoryStore", "Store"]

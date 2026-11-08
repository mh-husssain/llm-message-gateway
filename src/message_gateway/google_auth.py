"""Service-account credentials from GOOGLE_SERVICE_ACCOUNT_JSON.

The variable may hold either a path to a key file or the key's JSON inline
(the latter is convenient when the value comes from a secret manager).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from google.oauth2.service_account import Credentials

SHEETS_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",
)


def service_account_credentials(
    value: str, scopes: Sequence[str], *, subject: str | None = None
) -> Credentials:
    """Build credentials; `subject` impersonates a user via domain-wide delegation."""
    v = (value or "").strip()
    if v.startswith("{"):
        creds = Credentials.from_service_account_info(json.loads(v), scopes=list(scopes))
    else:
        creds = Credentials.from_service_account_file(v, scopes=list(scopes))
    return creds.with_subject(subject) if subject else creds

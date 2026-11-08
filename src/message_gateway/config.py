"""Runtime configuration, read from environment variables.

Nothing here has a deployment-specific default: identifiers, addresses and
model names must come from the environment (see `.env.example`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields


class ConfigError(RuntimeError):
    """Raised when a required setting is missing."""


# Setting name -> environment variable.
_ENV_NAMES = {
    "timezone": "GATEWAY_TIMEZONE",
    "subject_prefix": "GATEWAY_SUBJECT_PREFIX",
    "sheet_id": "GATEWAY_SHEET_ID",
    "google_service_account": "GOOGLE_SERVICE_ACCOUNT_JSON",
    "anthropic_model": "ANTHROPIC_MODEL",
    "anthropic_fallback_model": "ANTHROPIC_FALLBACK_MODEL",
    "classifier_threshold": "CLASSIFIER_CONFIDENCE_THRESHOLD",
    "classifier_prompt_path": "CLASSIFIER_PROMPT_PATH",
    "gmail_user": "GMAIL_USER",
    "gmail_pubsub_topic": "GMAIL_PUBSUB_TOPIC",
    "gmail_watch_label_ids": "GMAIL_WATCH_LABEL_IDS",
}


@dataclass(frozen=True)
class Settings:
    timezone: str = "UTC"
    subject_prefix: str = "[Gateway]"
    sheet_id: str = ""
    # Either a path to a service-account key file or the key's JSON inline.
    google_service_account: str = ""
    anthropic_model: str = ""
    # Second-pass model; empty means "same as the primary model".
    anthropic_fallback_model: str = ""
    classifier_threshold: float = 0.65
    classifier_prompt_path: str = ""
    gmail_user: str = ""
    gmail_pubsub_topic: str = ""
    gmail_watch_label_ids: str = "INBOX"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}
        for f in fields(cls):
            raw = (env.get(_ENV_NAMES[f.name]) or "").strip()
            if not raw:
                continue
            values[f.name] = float(raw) if f.name == "classifier_threshold" else raw
        return cls(**values)

    def require(self, *names: str) -> None:
        missing = [_ENV_NAMES[n] for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError(f"Missing required environment variable(s): {', '.join(missing)}")

    @property
    def fallback_model(self) -> str:
        return self.anthropic_fallback_model or self.anthropic_model

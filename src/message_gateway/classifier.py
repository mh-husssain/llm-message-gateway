"""Two-pass intent classifier with Pydantic-validated, multi-intent output.

The model only turns free text into structured intents. It never sends a
message, never writes to the store and is never called for unknown senders.

Pass 1 uses the primary model. If its first intent is `unclear` or its
confidence is below the threshold (default 0.65), pass 2 runs with a fallback
addendum and the first attempt's output in the payload. The second answer is
kept only if it is strictly more confident, so a confident classification is
never silently downgraded. Any API or validation failure degrades to an
`unclear` result instead of raising.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

from pydantic import BaseModel, Field

from .clock import now_local
from .config import ConfigError, Settings
from .models import Contact, IncomingMessage, Project
from .text import has_completion_keyword, is_arabic

log = logging.getLogger("message_gateway.classifier")

IntentName = Literal[
    "assign_task",
    "task_update",
    "task_completion",
    "request_extension",
    "delay_acknowledgment",
    "create_project",
    "add_contact",
    "request_info",
    "end_of_day_report",
    "completion_confirmation_response",
    "external_eta_response",
    "external_completion_confirmation",
    "unclear",
]
INTENT_NAMES: tuple[str, ...] = get_args(IntentName)

DEFAULT_THRESHOLD = 0.65
MAX_TOKENS = 1500

# Appended to the system prompt for the second pass only.
FALLBACK_ADDENDUM = """\
## Fallback context

You are being invoked as a SECOND-PASS classifier because the first pass
either returned `unclear` or had low confidence. The first attempt's output
is included in the input under `prior_classifier_output`. Read it, but do
not defer to it; trust your own analysis.

Reconsider with extra attention to: [Redacted]

If you remain genuinely uncertain, still return `unclear` with honest
confidence; do not inflate confidence to satisfy the fallback. Output the
same JSON shape (no prose, no code fences)."""


class IntentResult(BaseModel):
    intent: IntentName
    confidence: float = Field(ge=0, le=1)
    extracted: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""


class ClassificationResult(BaseModel):
    """Always wraps one or more intents, in message order; the first is dominant."""

    intents: list[IntentResult] = Field(default_factory=list)

    @property
    def primary(self) -> IntentResult:
        if not self.intents:
            return IntentResult(
                intent="unclear",
                confidence=0.0,
                reasoning="Classifier returned an empty intents list.",
            )
        return self.intents[0]


def unclear_classification(reason: str) -> ClassificationResult:
    return ClassificationResult(
        intents=[IntentResult(intent="unclear", confidence=0.0, reasoning=reason)]
    )


class LLMClient(Protocol):
    """Anything that turns (model, system prompt, user message) into text."""

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str: ...


class AnthropicClient:
    """`LLMClient` over the Anthropic Messages API."""

    def __init__(self, client: Any = None) -> None:
        if client is None:
            from anthropic import Anthropic

            client = Anthropic()
        self._client = client

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str:
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text


def default_prompt_path() -> Path:
    override = (os.environ.get("CLASSIFIER_PROMPT_PATH") or "").strip()
    if override:
        return Path(override)
    # src/message_gateway/classifier.py -> repository root
    return Path(__file__).resolve().parents[2] / "prompts" / "classifier.md"


def load_prompt(path: str | Path | None = None) -> str:
    return Path(path or default_prompt_path()).read_text(encoding="utf-8")


def build_payload(
    msg: IncomingMessage,
    sender: Contact,
    projects: dict[str, Project],
    *,
    today: date,
    prior_attempt: IntentResult | None = None,
) -> dict[str, Any]:
    """The user-message JSON. Only names, roles and active project ids/names
    are shared with the model; no contact details."""
    full = f"{msg.subject}\n{msg.body}" if msg.subject else msg.body
    payload: dict[str, Any] = {
        "subject": msg.subject,
        "message": msg.body,
        "sender": {"name": sender.name, "role": sender.role, "permission": sender.permission},
        "active_projects": [
            {"id": p.id, "name": p.name} for p in projects.values() if p.status.lower() == "active"
        ],
        "language": "ar" if is_arabic(full) else "en",
        "mentions_completion_keyword": has_completion_keyword(full),
        "today_iso": today.isoformat(),
    }
    if prior_attempt is not None:
        payload["prior_classifier_output"] = {
            "intent": prior_attempt.intent,
            "confidence": prior_attempt.confidence,
            "reasoning": prior_attempt.reasoning,
        }
    return payload


class Classifier:
    def __init__(
        self,
        llm: LLMClient,
        *,
        prompt: str,
        primary_model: str,
        fallback_model: str | None = None,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self.llm = llm
        self.prompt = prompt
        self.primary_model = primary_model
        self.fallback_model = fallback_model or primary_model
        self.threshold = threshold

    @classmethod
    def from_env(cls, settings: Settings, llm: LLMClient | None = None) -> Classifier:
        if not settings.anthropic_model:
            raise ConfigError("Missing required environment variable(s): ANTHROPIC_MODEL")
        return cls(
            llm or AnthropicClient(),
            prompt=load_prompt(settings.classifier_prompt_path or None),
            primary_model=settings.anthropic_model,
            fallback_model=settings.fallback_model,
            threshold=settings.classifier_threshold,
        )

    def _classify_once(
        self, payload: dict[str, Any], *, model: str, system: str
    ) -> ClassificationResult:
        """One model call. Raises on API or validation error."""
        text = self.llm.complete(
            model=model,
            system=system,
            user=json.dumps(payload, ensure_ascii=False),
            max_tokens=MAX_TOKENS,
        )
        return ClassificationResult.model_validate_json(text.strip())

    def classify(
        self, msg: IncomingMessage, sender: Contact, projects: dict[str, Project]
    ) -> ClassificationResult:
        today = now_local().date()
        payload = build_payload(msg, sender, projects, today=today)

        try:
            primary = self._classify_once(payload, model=self.primary_model, system=self.prompt)
        except Exception as e:  # API, JSON and validation errors all degrade to unclear
            log.warning("primary classifier failed: %s", e)
            primary = unclear_classification(f"Primary error: {type(e).__name__}")

        primary_intent = primary.primary
        if primary_intent.intent != "unclear" and primary_intent.confidence >= self.threshold:
            return primary

        log.info(
            "escalating to fallback classifier (primary intent=%s conf=%.2f, threshold=%.2f, "
            "fallback model=%s)",
            primary_intent.intent,
            primary_intent.confidence,
            self.threshold,
            self.fallback_model,
        )
        fallback_payload = build_payload(
            msg, sender, projects, today=today, prior_attempt=primary_intent
        )
        try:
            fallback = self._classify_once(
                fallback_payload,
                model=self.fallback_model,
                system=self.prompt + "\n\n" + FALLBACK_ADDENDUM,
            )
        except Exception as e:
            log.warning("fallback classifier failed: %s", e)
            return primary

        fallback_intent = fallback.primary
        if fallback_intent.confidence > primary_intent.confidence:
            log.info(
                "fallback won: intent=%s conf=%.2f (primary was %s/%.2f)",
                fallback_intent.intent,
                fallback_intent.confidence,
                primary_intent.intent,
                primary_intent.confidence,
            )
            return fallback
        log.info(
            "fallback did not improve confidence (%s/%.2f vs primary %s/%.2f); keeping primary",
            fallback_intent.intent,
            fallback_intent.confidence,
            primary_intent.intent,
            primary_intent.confidence,
        )
        return primary

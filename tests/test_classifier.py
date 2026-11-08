"""Two-pass classifier logic and output validation."""

from __future__ import annotations

import json
import re
from datetime import date

import pytest
from pydantic import ValidationError

from fakes import ALICE, FakeLLM, msg, one
from message_gateway.classifier import (
    FALLBACK_ADDENDUM,
    INTENT_NAMES,
    AnthropicClient,
    ClassificationResult,
    Classifier,
    build_payload,
    default_prompt_path,
    load_prompt,
)
from message_gateway.config import ConfigError, Settings
from message_gateway.models import Contact, Project

SENDER = Contact(email=ALICE, phone="", name="Alice", role="Engineer", permission="employee")
PROJECTS = {
    "P-0001": Project(id="P-0001", name="Project Alpha", status="active"),
    "P-0003": Project(id="P-0003", name="Project Gamma", status="completed"),
}


def make(llm: FakeLLM, threshold: float = 0.65) -> Classifier:
    return Classifier(
        llm,
        prompt="SYSTEM",
        primary_model="primary-m",
        fallback_model="fallback-m",
        threshold=threshold,
    )


def run(llm: FakeLLM, body: str = "Done with the report", **kw):
    return make(llm, **kw).classify(msg(ALICE, body), SENDER, PROJECTS)


def test_confident_primary_is_returned_without_second_pass():
    llm = FakeLLM(one("task_completion", 0.9, task_id="T-0001"))
    result = run(llm)
    assert result.primary.intent == "task_completion"
    assert result.primary.extracted == {"task_id": "T-0001"}
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == "primary-m"
    assert llm.calls[0]["system"] == "SYSTEM"


def test_confidence_exactly_at_threshold_is_confident():
    llm = FakeLLM(one("task_update", 0.65))
    assert run(llm).primary.intent == "task_update"
    assert len(llm.calls) == 1


def test_unclear_primary_escalates_and_more_confident_fallback_wins():
    llm = FakeLLM(one("unclear", 0.3), one("request_info", 0.8, scope="my_tasks"))
    result = run(llm)
    assert result.primary.intent == "request_info"
    assert len(llm.calls) == 2
    second = llm.calls[1]
    assert second["model"] == "fallback-m"
    assert second["system"] == "SYSTEM\n\n" + FALLBACK_ADDENDUM
    assert second["user"]["prior_classifier_output"] == {
        "intent": "unclear",
        "confidence": 0.3,
        "reasoning": "stub",
    }
    assert "prior_classifier_output" not in llm.calls[0]["user"]


def test_low_confidence_non_unclear_also_escalates():
    llm = FakeLLM(one("assign_task", 0.5), one("assign_task", 0.9))
    result = run(llm)
    assert len(llm.calls) == 2
    assert result.primary.confidence == 0.9


def test_fallback_that_is_not_more_confident_keeps_primary():
    llm = FakeLLM(one("assign_task", 0.6), one("unclear", 0.6))
    result = run(llm)
    assert result.primary.intent == "assign_task"
    assert result.primary.confidence == 0.6


def test_invalid_json_from_primary_degrades_to_unclear_then_fallback():
    llm = FakeLLM("not json at all", one("task_update", 0.7))
    result = run(llm)
    assert len(llm.calls) == 2
    assert llm.calls[1]["user"]["prior_classifier_output"]["reasoning"] == (
        "Primary error: ValidationError"
    )
    assert result.primary.intent == "task_update"


def test_api_error_on_both_passes_returns_unclear():
    llm = FakeLLM(RuntimeError("timeout"), RuntimeError("timeout"))
    result = run(llm)
    assert result.primary.intent == "unclear"
    assert result.primary.confidence == 0.0
    assert result.primary.reasoning == "Primary error: RuntimeError"


def test_fallback_error_keeps_primary():
    llm = FakeLLM(one("task_update", 0.4), RuntimeError("boom"))
    result = run(llm)
    assert result.primary.intent == "task_update"
    assert result.primary.confidence == 0.4


@pytest.mark.parametrize(
    "payload",
    [
        one("not_an_intent", 0.9),
        one("task_update", 1.5),
        one("task_update", -0.1),
        {"intents": [{"intent": "task_update"}]},  # confidence missing
    ],
)
def test_schema_violations_are_rejected(payload):
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate_json(json.dumps(payload))


def test_schema_violation_from_model_is_treated_as_unclear():
    llm = FakeLLM(one("task_update", 1.5), one("bogus", 0.9))
    result = run(llm)
    assert result.primary.intent == "unclear"


def test_multi_intent_output_preserves_order():
    payload = {
        "intents": [
            {"intent": "create_project", "confidence": 0.9, "extracted": {"proposed_name": "X"}},
            {"intent": "assign_task", "confidence": 0.85, "extracted": {}},
        ]
    }
    result = run(FakeLLM(payload))
    assert [i.intent for i in result.intents] == ["create_project", "assign_task"]
    assert result.primary.intent == "create_project"


def test_empty_intents_list_reads_as_unclear_and_escalates():
    llm = FakeLLM({"intents": []}, {"intents": []})
    result = run(llm)
    assert len(llm.calls) == 2
    assert result.primary.intent == "unclear"


def test_payload_shares_only_names_roles_and_active_projects():
    m = msg(ALICE, "تم الانتهاء", subject="Re: report")
    payload = build_payload(m, SENDER, PROJECTS, today=date(2026, 5, 4))
    assert payload["sender"] == {"name": "Alice", "role": "Engineer", "permission": "employee"}
    assert payload["active_projects"] == [{"id": "P-0001", "name": "Project Alpha"}]
    assert payload["language"] == "ar"
    assert payload["mentions_completion_keyword"] is True
    assert payload["today_iso"] == "2026-05-04"
    assert ALICE not in str(payload)


def test_prompt_file_lists_every_intent_and_keeps_sections():
    prompt = load_prompt()
    for name in INTENT_NAMES:
        assert f"**{name}**" in prompt
    for heading in (
        "## Input",
        "## Output",
        "## Intent definitions",
        "## Rules you MUST follow",
        "## Examples",
    ):
        assert heading in prompt
    literal = re.search(r'"intent": (.+),\n', prompt).group(1)
    assert set(re.findall(r'"(\w+)"', literal)) == set(INTENT_NAMES)
    assert "[Redacted]" in prompt


def test_prompt_path_can_be_overridden(monkeypatch, tmp_path):
    custom = tmp_path / "p.md"
    custom.write_text("CUSTOM", encoding="utf-8")
    monkeypatch.setenv("CLASSIFIER_PROMPT_PATH", str(custom))
    assert default_prompt_path() == custom
    assert load_prompt() == "CUSTOM"


def test_from_env_requires_a_model():
    with pytest.raises(ConfigError, match="ANTHROPIC_MODEL"):
        Classifier.from_env(Settings(), llm=FakeLLM())


def test_from_env_defaults_fallback_to_primary_model():
    settings = Settings.from_env(
        {"ANTHROPIC_MODEL": "m1", "CLASSIFIER_CONFIDENCE_THRESHOLD": "0.8"}
    )
    c = Classifier.from_env(settings, llm=FakeLLM())
    assert (c.primary_model, c.fallback_model, c.threshold) == ("m1", "m1", 0.8)


def test_anthropic_adapter_maps_the_messages_call():
    class Resp:
        content = [type("Block", (), {"text": '{"intents": []}'})()]

    class Messages:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return Resp()

    sdk = type("SDK", (), {})()
    sdk.messages = Messages()
    text = AnthropicClient(sdk).complete(model="m", system="s", user="u", max_tokens=10)
    assert text == '{"intents": []}'
    assert sdk.messages.kwargs == {
        "model": "m",
        "max_tokens": 10,
        "system": "s",
        "messages": [{"role": "user", "content": "u"}],
    }

"""Side-by-side parity tests: ``../../lambda_handler.lambda_handler``
vs ``../lambda_function.lambda_handler``.

This is the gate that protects against subtle behavior drift during the
refactor. For each scenario we:

1. Build one synthetic Lex event.
2. Configure the same fake Bedrock + DDB responses on both stacks.
3. Run both handlers against the same event.
4. Assert the response payloads match on the dimensions that Connect
   actually cares about: sessionState.dialogAction.type,
   sessionState.intent.name/state, sessionState.sessionAttributes,
   messages[0].contentType + content.

Anything that diverges is either a bug in the agentic refactor or an
intentional improvement -- both deserve to be reviewed explicitly
before cutover.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The original prod Lambda lives in ``../Lex/`` -- the frozen v1 archive
# kept as the parity baseline after the 2026-05-23 cutover to the agentic
# stack. Make it importable. (conftest already added AgenticRAG to
# sys.path; we add the Lex/ archive here so ``import lambda_handler``
# resolves to the v1 file.)
PROD_DIR = Path(__file__).resolve().parents[2] / "Lex"
if str(PROD_DIR) not in sys.path:
    sys.path.insert(0, str(PROD_DIR))


import lambda_handler as prod_handler  # noqa: E402
import lambda_function as agentic_handler  # noqa: E402  (our refactor)
import agentic.aws_clients as agentic_aws  # noqa: E402


# ---------------------------------------------------------------------------
# Shared mocks: replace BOTH prod and agentic boto3 clients with the same
# MagicMock so per-test configuration applies to both stacks identically.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def shared_bedrock_mock():
    """Replace ``lambda_handler.bedrock_agent`` AND
    ``agentic.aws_clients.bedrock_agent`` with the same fresh MagicMock so
    each test configures one object and both stacks see the same response."""

    bedrock = MagicMock(name="shared_bedrock")
    with patch.object(prod_handler, "bedrock_agent", bedrock), patch.object(
        agentic_aws, "bedrock_agent", bedrock
    ):
        yield bedrock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lex_event(
    *,
    intent_name="PrepQuestionIntent",
    invocation_source="FulfillmentCodeHook",
    transcript="when should I start my prep?",
    lang="en",
    extra_attrs=None,
    slots=None,
):
    attrs = {"langCode": lang}
    if extra_attrs:
        attrs.update(extra_attrs)
    return {
        "invocationSource": invocation_source,
        "inputTranscript": transcript,
        "sessionId": "parity-session-id",
        "sessionState": {
            "sessionAttributes": attrs,
            "intent": {"name": intent_name, "slots": slots or {}},
        },
    }


def _configure_bedrock_grounded(bedrock, *, text, top_score=0.71):
    """Both retrieve (Stage 1 gate) and retrieve_and_generate (Stage 2)
    succeed with a chunk above the threshold."""
    bedrock.retrieve.return_value = {
        "retrievalResults": [{"score": top_score, "content": {"text": "stub chunk"}}]
    }
    bedrock.retrieve_and_generate.return_value = {"output": {"text": text}}


def _configure_bedrock_blocked(bedrock):
    """Stage 1 returns chunks below the threshold -> grounding gate fires."""
    bedrock.retrieve.return_value = {
        "retrievalResults": [{"score": 0.12, "content": {"text": "off-topic stub"}}]
    }
    # retrieve_and_generate should never be called in this path.
    bedrock.retrieve_and_generate.side_effect = AssertionError(
        "retrieve_and_generate must not run when grounding gate blocks"
    )


def _configure_bedrock_error(bedrock):
    """Stage 1 retrieve raises -> handler returns bedrock_error_template."""
    bedrock.retrieve.side_effect = RuntimeError("boom")
    bedrock.retrieve_and_generate.side_effect = RuntimeError("boom")


def _normalised(resp):
    """Strip transient bits so two structurally-equal responses compare equal.

    We don't care about activeContexts (neither handler emits them when
    absent on input) or about list ordering -- only the actual payload
    fields Connect reads."""
    ss = resp.get("sessionState") or {}
    return {
        "dialogAction": ss.get("dialogAction"),
        "intent_name": (ss.get("intent") or {}).get("name"),
        "intent_state": (ss.get("intent") or {}).get("state"),
        "session_attributes": dict(ss.get("sessionAttributes") or {}),
        "messages": [
            {
                "contentType": m.get("contentType"),
                "content": m.get("content"),
            }
            for m in (resp.get("messages") or [])
        ],
    }


def _assert_parity(event, bedrock=None):
    """Run both handlers and assert the responses normalise to the same dict."""
    prod_resp = prod_handler.lambda_handler(event, None)
    # Reset side_effects so the same bedrock mock can be reused on the
    # second call without sticky exceptions.
    if bedrock is not None:
        # Preserve the configured return_value but reset call records.
        bedrock.retrieve.reset_mock(side_effect=False)
        bedrock.retrieve_and_generate.reset_mock(side_effect=False)
    agentic_resp = agentic_handler.lambda_handler(event, None)
    assert _normalised(agentic_resp) == _normalised(prod_resp), (
        f"\nPROD:    {_normalised(prod_resp)}\n"
        f"AGENTIC: {_normalised(agentic_resp)}"
    )
    return prod_resp, agentic_resp


# ===========================================================================
# Parity scenarios
# ===========================================================================


# --- Q&A path -----------------------------------------------------------------


def test_parity_qa_grounded_answer_en(shared_bedrock_mock):
    _configure_bedrock_grounded(
        shared_bedrock_mock, text="Drink the prep at 6 PM.", top_score=0.71
    )
    event = _lex_event(transcript="when should I start my prep?")
    _assert_parity(event, shared_bedrock_mock)


def test_parity_qa_grounded_answer_es(shared_bedrock_mock):
    _configure_bedrock_grounded(
        shared_bedrock_mock, text="Empieza a las 6 PM.", top_score=0.65
    )
    event = _lex_event(transcript="¿cuándo empiezo mi preparación?", lang="es")
    _assert_parity(event, shared_bedrock_mock)


def test_parity_qa_grounding_blocked_returns_no_answer_fallback(shared_bedrock_mock):
    _configure_bedrock_blocked(shared_bedrock_mock)
    event = _lex_event(transcript="who won the world cup?")
    _assert_parity(event, shared_bedrock_mock)


def test_parity_qa_bedrock_error_returns_error_template(shared_bedrock_mock):
    _configure_bedrock_error(shared_bedrock_mock)
    event = _lex_event(transcript="when should I start?")
    _assert_parity(event, shared_bedrock_mock)


def test_parity_qa_empty_input_returns_canned_prompt(shared_bedrock_mock):
    # No Bedrock should be called for empty input.
    event = _lex_event(transcript="")
    _assert_parity(event, shared_bedrock_mock)
    shared_bedrock_mock.retrieve.assert_not_called()
    shared_bedrock_mock.retrieve_and_generate.assert_not_called()


def test_parity_qa_goodbye_closes_conversation(shared_bedrock_mock):
    event = _lex_event(transcript="goodbye")
    _assert_parity(event, shared_bedrock_mock)
    shared_bedrock_mock.retrieve.assert_not_called()


def test_parity_qa_goodbye_es(shared_bedrock_mock):
    event = _lex_event(transcript="adiós", lang="es")
    _assert_parity(event, shared_bedrock_mock)


def test_parity_qa_escalation_en(shared_bedrock_mock):
    event = _lex_event(transcript="I have chest pain")
    _assert_parity(event, shared_bedrock_mock)
    shared_bedrock_mock.retrieve.assert_not_called()


def test_parity_qa_escalation_es(shared_bedrock_mock):
    event = _lex_event(transcript="Tengo dolor de pecho", lang="es")
    _assert_parity(event, shared_bedrock_mock)


def test_parity_qa_with_caller_context_passes_blurb_to_bedrock(shared_bedrock_mock):
    """Both stacks must call retrieve_and_generate with the patient blurb
    included in the augmented query (same text, both stacks)."""
    _configure_bedrock_grounded(shared_bedrock_mock, text="Drink at 6 PM.")
    event = _lex_event(
        extra_attrs={
            "patientName": "Tejodhay",
            "procedureDate": "2026-05-22",
            "procedureTime": "09:00",
        },
    )
    _assert_parity(event, shared_bedrock_mock)


# --- Collection intents (FulfillmentCodeHook) --------------------------------


def test_parity_collect_name_intent_en(shared_bedrock_mock):
    event = _lex_event(
        intent_name="CollectNameIntent",
        transcript="my name is Tejodhay",
    )
    _assert_parity(event, shared_bedrock_mock)


def test_parity_collect_name_intent_es(shared_bedrock_mock):
    event = _lex_event(
        intent_name="CollectNameIntent",
        transcript="me llamo Tejodhay",
        lang="es",
    )
    _assert_parity(event, shared_bedrock_mock)


def test_parity_collect_name_intent_skip_dtmf(shared_bedrock_mock):
    event = _lex_event(intent_name="CollectNameIntent", transcript="#")
    _assert_parity(event, shared_bedrock_mock)


def test_parity_collect_name_intent_empty_es_skip(shared_bedrock_mock):
    event = _lex_event(intent_name="CollectNameIntent", transcript="", lang="es")
    _assert_parity(event, shared_bedrock_mock)


def test_parity_collect_date_intent_raw(shared_bedrock_mock):
    event = _lex_event(
        intent_name="CollectDateIntent",
        transcript="May twenty second twenty twenty six",
    )
    _assert_parity(event, shared_bedrock_mock)


def test_parity_collect_time_intent_raw(shared_bedrock_mock):
    event = _lex_event(
        intent_name="CollectTimeIntent",
        transcript="nine in the morning",
    )
    _assert_parity(event, shared_bedrock_mock)


# --- CollectNameIntent DialogCodeHook ---------------------------------------


def test_parity_name_dialog_slot_filled(shared_bedrock_mock):
    event = _lex_event(
        intent_name="CollectNameIntent",
        invocation_source="DialogCodeHook",
        transcript="Tejodhay",
        slots={"patientName": {"value": {"interpretedValue": "Tejodhay"}}},
    )
    _assert_parity(event, shared_bedrock_mock)


def test_parity_name_dialog_first_attempt_offers_spell_by_letter(shared_bedrock_mock):
    event = _lex_event(
        intent_name="CollectNameIntent",
        invocation_source="DialogCodeHook",
        transcript="",
        slots={"patientName": None},
    )
    _assert_parity(event, shared_bedrock_mock)


def test_parity_name_dialog_second_attempt_gives_up(shared_bedrock_mock):
    event = _lex_event(
        intent_name="CollectNameIntent",
        invocation_source="DialogCodeHook",
        transcript="",
        extra_attrs={"nameDialogRetry": "1"},
        slots={"patientName": None},
    )
    _assert_parity(event, shared_bedrock_mock)


def test_parity_name_dialog_spanish_skips_spell_by_letter(shared_bedrock_mock):
    event = _lex_event(
        intent_name="CollectNameIntent",
        invocation_source="DialogCodeHook",
        transcript="",
        lang="es",
        slots={"patientName": None},
    )
    _assert_parity(event, shared_bedrock_mock)


# --- FallbackIntent guard (Solution 2) --------------------------------------


def test_parity_fallback_with_collection_mode_name(shared_bedrock_mock):
    """Bare-name utterance misclassified as FallbackIntent must be
    rerouted to the collection path so patientNameRaw still lands."""
    event = _lex_event(
        intent_name="FallbackIntent",
        transcript="Tejodhay",
        extra_attrs={"collectionMode": "name"},
    )
    _assert_parity(event, shared_bedrock_mock)


def test_parity_fallback_without_collection_mode_returns_empty_messages(
    shared_bedrock_mock,
):
    event = _lex_event(
        intent_name="FallbackIntent",
        transcript="who won the football game?",
    )
    _assert_parity(event, shared_bedrock_mock)

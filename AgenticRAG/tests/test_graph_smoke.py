"""End-to-end smoke tests that drive the compiled LangGraph (no real
Bedrock / DDB -- ``conftest.py`` swaps both clients for MagicMocks)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agentic import compiled_graph


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
        "sessionId": "smoke-session-id",
        "sessionState": {
            "sessionAttributes": attrs,
            "intent": {"name": intent_name, "slots": slots or {}},
        },
    }


def _patch_rag(text="Drink the prep at 6 PM.", top_score=0.71, grounded=True):
    """Decorator-friendly helper to stub agentic.rag.retrieve_and_generate."""
    from agentic.rag import RAGResult

    return patch(
        "agentic.rag.retrieve_and_generate",
        return_value=RAGResult(text=text, top_score=top_score, grounded=grounded),
    )


# ---------------------------------------------------------------------------
# Q&A pipeline end-to-end
# ---------------------------------------------------------------------------


def test_graph_qa_happy_path_returns_grounded_answer_plus_follow_up():
    event = _lex_event(transcript="when should I start my prep?")
    with _patch_rag(text="Start at 6 PM."):
        final = compiled_graph.invoke({"event": event})
    resp = final["response"]
    msg = resp["messages"][0]["content"]
    assert "Start at 6 PM." in msg
    assert "What else can I help with?" in msg
    assert resp["sessionState"]["dialogAction"]["type"] == "Close"
    assert resp["sessionState"]["intent"]["name"] == "PrepQuestionIntent"
    assert final["branch"] == "qa/kb_search"
    assert final["retrieval_top_score"] == pytest.approx(0.71)


def test_graph_qa_spanish_uses_spanish_follow_up():
    event = _lex_event(transcript="¿cuándo empiezo mi preparación?", lang="es")
    with _patch_rag(text="Empieza a las 6 PM."):
        final = compiled_graph.invoke({"event": event})
    assert "¿En qué más puedo ayudarte?" in final["response"]["messages"][0]["content"]


def test_graph_qa_grounding_blocked_returns_no_answer_fallback():
    event = _lex_event(transcript="who won the world cup?")
    with _patch_rag(
        text="I could not find a clear answer for that in the approved prep documents. Please rephrase your GI prep question.",
        top_score=0.12,
        grounded=False,
    ):
        final = compiled_graph.invoke({"event": event})
    msg = final["response"]["messages"][0]["content"]
    assert "rephrase" in msg
    assert final["grounding_blocked"] is True


def test_graph_qa_bedrock_exception_falls_back_to_canned_error():
    event = _lex_event(transcript="when should I start?")
    with patch("agentic.rag.retrieve_and_generate", side_effect=RuntimeError("boom")):
        final = compiled_graph.invoke({"event": event})
    msg = final["response"]["messages"][0]["content"]
    assert "could not reach the medical knowledge service" in msg
    assert "(boom)" in msg


def test_graph_qa_empty_input_short_circuits_to_canned_fallback():
    event = _lex_event(transcript="")
    with _patch_rag() as mock_rag:
        final = compiled_graph.invoke({"event": event})
    msg = final["response"]["messages"][0]["content"]
    assert "didn't catch that" in msg
    mock_rag.assert_not_called()  # critical: never invoke RAG on empty input
    assert final["branch"] == "qa/empty_input"


def test_graph_qa_goodbye_closes_without_follow_up():
    event = _lex_event(transcript="goodbye")
    with _patch_rag() as mock_rag:
        final = compiled_graph.invoke({"event": event})
    msg = final["response"]["messages"][0]["content"]
    assert msg == "Thank you for calling. Goodbye."
    assert "What else" not in msg
    mock_rag.assert_not_called()
    assert final["close_conversation"] is True


def test_graph_qa_escalation_closes_without_follow_up():
    event = _lex_event(transcript="I have chest pain")
    with _patch_rag() as mock_rag:
        final = compiled_graph.invoke({"event": event})
    msg = final["response"]["messages"][0]["content"]
    assert "safety" in msg.lower()
    assert "What else" not in msg
    mock_rag.assert_not_called()


def test_graph_qa_escalation_spanish():
    event = _lex_event(transcript="Tengo dolor de pecho", lang="es")
    with _patch_rag() as mock_rag:
        final = compiled_graph.invoke({"event": event})
    msg = final["response"]["messages"][0]["content"]
    assert "seguridad" in msg.lower()
    mock_rag.assert_not_called()


# ---------------------------------------------------------------------------
# Short-circuit branches end-to-end
# ---------------------------------------------------------------------------


def test_graph_collect_name_intent_writes_session_attribute():
    event = _lex_event(
        intent_name="CollectNameIntent",
        transcript="my name is Tejodhay",
    )
    with _patch_rag() as mock_rag:
        final = compiled_graph.invoke({"event": event})
    resp = final["response"]
    attrs = resp["sessionState"]["sessionAttributes"]
    assert attrs["patientNameRaw"] == "Tejodhay"
    assert attrs.get("collectionMode") == ""
    assert resp["messages"] == []
    mock_rag.assert_not_called()


def test_graph_collect_date_intent_writes_raw_transcript():
    event = _lex_event(intent_name="CollectDateIntent", transcript="May twenty second")
    with _patch_rag() as mock_rag:
        final = compiled_graph.invoke({"event": event})
    attrs = final["response"]["sessionState"]["sessionAttributes"]
    assert attrs["procedureDateRaw"] == "May twenty second"
    mock_rag.assert_not_called()


def test_graph_name_dialog_first_attempt_offers_spell_by_letter():
    event = _lex_event(
        intent_name="CollectNameIntent",
        invocation_source="DialogCodeHook",
        transcript="",
        slots={"patientName": None},
    )
    final = compiled_graph.invoke({"event": event})
    da = final["response"]["sessionState"]["dialogAction"]
    assert da["type"] == "ElicitSlot"
    assert da["slotElicitationStyle"] == "SpellByLetter"


def test_graph_name_dialog_slot_filled_advances_to_fulfillment():
    event = _lex_event(
        intent_name="CollectNameIntent",
        invocation_source="DialogCodeHook",
        transcript="Tejodhay",
        slots={"patientName": {"value": {"interpretedValue": "Tejodhay"}}},
    )
    final = compiled_graph.invoke({"event": event})
    assert (
        final["response"]["sessionState"]["dialogAction"]["type"] == "FulfillIntent"
    )


def test_graph_fallback_intent_during_collection_name_routes_to_collection():
    """Bare-name utterance misclassified to FallbackIntent should be
    rerouted to the name-collection path when collectionMode=name."""
    event = _lex_event(
        intent_name="FallbackIntent",
        transcript="Tejodhay",
        extra_attrs={"collectionMode": "name"},
    )
    final = compiled_graph.invoke({"event": event})
    attrs = final["response"]["sessionState"]["sessionAttributes"]
    assert attrs["patientNameRaw"] == "Tejodhay"
    assert attrs.get("collectionMode") == ""
    # IntentName reported back to Connect is CollectNameIntent so the
    # Save_Name block fires on the expected branch.
    assert final["response"]["sessionState"]["intent"]["name"] == "CollectNameIntent"


def test_graph_fallback_intent_during_qa_returns_empty_messages():
    """FallbackIntent OUTSIDE collection mode must NOT speak -- the
    Connect flow's GI_Check_Fallback block owns the spoken message."""
    event = _lex_event(
        intent_name="FallbackIntent",
        transcript="who won the football game?",
    )
    final = compiled_graph.invoke({"event": event})
    resp = final["response"]
    assert resp["messages"] == []
    assert resp["sessionState"]["dialogAction"]["type"] == "Close"
    assert resp["sessionState"]["intent"]["name"] == "FallbackIntent"


# ---------------------------------------------------------------------------
# Caller-info threading
# ---------------------------------------------------------------------------


def test_graph_qa_passes_caller_info_to_bedrock():
    event = _lex_event(
        extra_attrs={
            "patientName": "Tejodhay",
            "procedureDate": "2026-05-22",
            "procedureTime": "09:00",
        },
    )
    with _patch_rag() as mock_rag:
        compiled_graph.invoke({"event": event})
    _, kwargs = mock_rag.call_args
    blurb = kwargs["patient_blurb"]
    assert "Tejodhay" in blurb
    assert "2026-05-22" in blurb
    assert "09:00" in blurb


def test_graph_qa_skips_placeholder_caller_info():
    event = _lex_event(
        extra_attrs={
            "patientName": "not provided",
            "procedureDate": "2026-05-22",
            "procedureTime": "no proporcionado",
        },
    )
    with _patch_rag() as mock_rag:
        compiled_graph.invoke({"event": event})
    blurb = mock_rag.call_args.kwargs["patient_blurb"]
    assert "not provided" not in blurb
    assert "no proporcionado" not in blurb
    assert "2026-05-22" in blurb

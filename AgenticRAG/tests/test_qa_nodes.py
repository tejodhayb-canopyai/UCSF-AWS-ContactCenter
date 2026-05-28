"""Unit tests for the Q&A pipeline nodes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agentic.nodes import qa
from agentic.nodes.setup import setup_node


def _run_setup(event):
    return {"event": event, **setup_node({"event": event})}


def _qa_state(utterance, lang="en", session_attrs=None, intent_name="PrepQuestionIntent"):
    attrs = {"langCode": lang}
    if session_attrs:
        attrs.update(session_attrs)
    event = {
        "invocationSource": "FulfillmentCodeHook",
        "inputTranscript": utterance,
        "sessionId": "test-session-id",
        "sessionState": {
            "sessionAttributes": attrs,
            "intent": {"name": intent_name, "slots": {}},
        },
    }
    state = _run_setup(event)
    state.update(qa.qa_context_node(state))
    return state


# ---------------------------------------------------------------------------
# qa_classify
# ---------------------------------------------------------------------------


def test_qa_classify_empty_input():
    state = _qa_state("")
    assert qa.qa_classify(state) == qa.QA_EMPTY


def test_qa_classify_end_conversation_en():
    state = _qa_state("goodbye")
    assert qa.qa_classify(state) == qa.QA_END


def test_qa_classify_end_conversation_es():
    state = _qa_state("adiós", lang="es")
    assert qa.qa_classify(state) == qa.QA_END


def test_qa_classify_escalation_en():
    state = _qa_state("I have chest pain")
    assert qa.qa_classify(state) == qa.QA_ESCALATE


def test_qa_classify_escalation_es():
    state = _qa_state("Tengo dolor de pecho", lang="es")
    assert qa.qa_classify(state) == qa.QA_ESCALATE


def test_qa_classify_normal_question_routes_to_answer():
    state = _qa_state("when should I start my prep?")
    assert qa.qa_classify(state) == qa.QA_ANSWER


def test_qa_classify_priority_empty_beats_everything():
    """Empty input wins over everything (would-be-escalate text that's
    whitespace shouldn't escalate)."""
    state = _qa_state("   \n  ")
    assert qa.qa_classify(state) == qa.QA_EMPTY


# ---------------------------------------------------------------------------
# Canned answer nodes
# ---------------------------------------------------------------------------


def test_empty_input_node_uses_skill_canned_string():
    state = _qa_state("")
    out = qa.empty_input_node(state)
    assert "didn't catch that" in out["answer"]
    assert out["close_conversation"] is False
    assert out["grounding_blocked"] is True
    assert out["branch"] == "qa/empty_input"


def test_end_conversation_node_closes():
    state = _qa_state("goodbye")
    out = qa.end_conversation_node(state)
    assert out["close_conversation"] is True
    assert "Goodbye" in out["answer"]


def test_escalation_node_closes():
    state = _qa_state("I have chest pain")
    out = qa.escalation_node(state)
    assert out["close_conversation"] is True
    assert "safety" in out["answer"].lower()


def test_end_conversation_node_spanish():
    state = _qa_state("adiós", lang="es")
    out = qa.end_conversation_node(state)
    assert out["answer"] == "Gracias por llamar. Adiós."


def test_escalation_node_spanish():
    state = _qa_state("Tengo dolor de pecho", lang="es")
    out = qa.escalation_node(state)
    assert "seguridad" in out["answer"].lower()


# ---------------------------------------------------------------------------
# kb_search_node (mocked Bedrock)
# ---------------------------------------------------------------------------


def test_kb_search_happy_path_returns_grounded_answer():
    state = _qa_state("when should I start my prep?")
    with patch("agentic.rag.retrieve_and_generate") as mock_rag:
        from agentic.rag import RAGResult
        mock_rag.return_value = RAGResult(
            text="Start your prep at 6 PM the day before.",
            top_score=0.72,
            grounded=True,
        )
        out = qa.kb_search_node(state)
    assert out["answer"] == "Start your prep at 6 PM the day before."
    assert out["retrieval_top_score"] == 0.72
    assert out["grounding_blocked"] is False
    assert out["close_conversation"] is False
    assert out["branch"] == "qa/kb_search"
    # Verify rag was called with the right skill + utterance
    args, kwargs = mock_rag.call_args
    assert kwargs["user_question"] == "when should I start my prep?"
    assert kwargs["skill"].language == "en"


def test_kb_search_grounding_blocked_returns_no_answer_fallback():
    state = _qa_state("what is the capital of France?")
    with patch("agentic.rag.retrieve_and_generate") as mock_rag:
        from agentic.rag import RAGResult
        mock_rag.return_value = RAGResult(
            text=state["skill"].canned.no_answer_fallback,
            top_score=0.12,
            grounded=False,
        )
        out = qa.kb_search_node(state)
    assert out["grounding_blocked"] is True
    assert "rephrase" in out["answer"]


def test_kb_search_exception_returns_bedrock_error_template():
    state = _qa_state("when should I start my prep?")
    with patch("agentic.rag.retrieve_and_generate", side_effect=RuntimeError("boom")):
        out = qa.kb_search_node(state)
    assert out["grounding_blocked"] is True
    assert "(boom)" in out["answer"]
    assert out["branch"] == "qa/kb_search_error"


def test_kb_search_passes_patient_blurb_when_caller_info_present():
    """Verify the caller_info is included in the prompt to Bedrock."""
    event = {
        "invocationSource": "FulfillmentCodeHook",
        "inputTranscript": "when should I start my prep?",
        "sessionId": "s1",
        "sessionState": {
            "sessionAttributes": {
                "langCode": "en",
                "patientName": "Tejodhay",
                "procedureDate": "2026-05-22",
                "procedureTime": "09:00",
            },
            "intent": {"name": "PrepQuestionIntent", "slots": {}},
        },
    }
    state = _run_setup(event)
    state.update(qa.qa_context_node(state))
    with patch("agentic.rag.retrieve_and_generate") as mock_rag:
        from agentic.rag import RAGResult
        mock_rag.return_value = RAGResult(text="ok", top_score=0.9, grounded=True)
        qa.kb_search_node(state)
    _, kwargs = mock_rag.call_args
    assert "Tejodhay" in kwargs["patient_blurb"]
    assert "2026-05-22" in kwargs["patient_blurb"]
    assert "09:00" in kwargs["patient_blurb"]


# ---------------------------------------------------------------------------
# post_process
# ---------------------------------------------------------------------------


def test_post_process_collapses_whitespace_and_strips_prefix():
    state = {"answer": "Answer:  drink the prep  slowly."}
    out = qa.post_process_node(state)
    assert out["answer"] == "drink the prep slowly."


# ---------------------------------------------------------------------------
# finalize_node
# ---------------------------------------------------------------------------


def test_finalize_appends_follow_up_for_normal_answer():
    state = _qa_state("when should I start my prep?")
    state.update({"answer": "Drink at 6 PM.", "close_conversation": False})
    out = qa.finalize_node(state)
    assert "Drink at 6 PM." in out["spoken"]
    assert "What else can I help with?" in out["spoken"]
    assert out["response"]["sessionState"]["dialogAction"]["type"] == "Close"
    msg = out["response"]["messages"][0]
    assert msg["contentType"] == "PlainText"
    assert "What else" in msg["content"]


def test_finalize_no_follow_up_for_close_conversation():
    state = _qa_state("goodbye")
    state.update({"answer": "Thank you for calling. Goodbye.", "close_conversation": True})
    out = qa.finalize_node(state)
    assert out["spoken"] == "Thank you for calling. Goodbye."
    assert "What else" not in out["spoken"]


def test_finalize_preserves_intent_name():
    state = _qa_state("when should I start my prep?", intent_name="PrepQuestionIntent")
    state.update({"answer": "abc", "close_conversation": False})
    out = qa.finalize_node(state)
    assert out["response"]["sessionState"]["intent"]["name"] == "PrepQuestionIntent"
    assert out["response"]["sessionState"]["intent"]["state"] == "Fulfilled"


def test_finalize_uses_spanish_follow_up():
    state = _qa_state("¿cuándo empiezo mi preparación?", lang="es")
    state.update({"answer": "Empieza a las 6 PM.", "close_conversation": False})
    out = qa.finalize_node(state)
    assert "¿En qué más puedo ayudarte?" in out["spoken"]


def test_finalize_preserves_session_attributes():
    state = _qa_state(
        "when should I start my prep?",
        session_attrs={"patientName": "Tejodhay", "procedureDate": "2026-05-22"},
    )
    state.update({"answer": "abc", "close_conversation": False})
    out = qa.finalize_node(state)
    attrs = out["response"]["sessionState"]["sessionAttributes"]
    assert attrs["patientName"] == "Tejodhay"
    assert attrs["procedureDate"] == "2026-05-22"


# ---------------------------------------------------------------------------
# audit_log_node (verifies best-effort: doesn't raise even with mock errors)
# ---------------------------------------------------------------------------


def test_audit_log_node_does_not_raise_when_table_unset(monkeypatch):
    monkeypatch.setattr("agentic.settings.CONVERSATION_TABLE_NAME", "")
    state = _qa_state("when should I start my prep?")
    state.update({"answer": "x", "spoken": "x What else?", "close_conversation": False})
    assert qa.audit_log_node(state) == {}


def test_audit_log_node_swallows_ddb_exceptions(monkeypatch):
    """A logging failure must never crash the call."""
    monkeypatch.setattr("agentic.settings.CONVERSATION_TABLE_NAME", "fake-table")
    with patch("agentic.aws_clients.ddb") as mock_ddb:
        mock_ddb.Table.return_value.put_item.side_effect = RuntimeError("ddb down")
        state = _qa_state("when should I start my prep?")
        state.update({"answer": "x", "spoken": "x What else?", "close_conversation": False})
        assert qa.audit_log_node(state) == {}

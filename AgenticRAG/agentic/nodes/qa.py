"""Q&A pipeline nodes.

These run for any non-collection, non-fallback-close intent (the bot's
"main job": grounded RAG answers to GI prep questions). Five branches
converge at :func:`finalize_node`:

    qa_context  -->  qa_classifier  -->  empty_input   ---+
                                  +-->  end_conversation -+--> finalize
                                  +-->  escalation        -+
                                  +-->  kb_search --> post_process -+

Only the kb_search branch incurs Bedrock cost; canned branches are
deterministic and free.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .. import audit, extractors, rag, skills_loader, text
from ..state import GraphState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context extraction (caller_phone, patient_id, caller_info, etc.)
# ---------------------------------------------------------------------------


def qa_context_node(state: GraphState) -> Dict[str, Any]:
    event = state["event"]
    skip_placeholders = extractors.all_skip_placeholders(
        skills_loader.all_skills().values()
    )

    caller_phone = extractors.extract_caller_phone(event)
    contact_id = extractors.extract_contact_id(event)
    patient_id = extractors.extract_patient_id(event)
    caller_info = extractors.extract_caller_info(event, skip_placeholders)
    session_id = event.get("sessionId") or contact_id

    return {
        "caller_phone": caller_phone,
        "contact_id": contact_id,
        "patient_id": patient_id,
        "caller_info": caller_info,
        "session_id": session_id,
        # Sensible defaults so finalize never sees missing keys for the
        # canned-answer branches (those nodes overwrite as needed).
        "retrieval_top_score": None,
        "grounding_blocked": False,
        "close_conversation": False,
    }


# ---------------------------------------------------------------------------
# Conditional edge: pick the right Q&A branch based on the utterance
# ---------------------------------------------------------------------------


QA_EMPTY = "qa_empty_input"
QA_END = "qa_end_conversation"
QA_ESCALATE = "qa_escalation"
QA_ANSWER = "qa_kb_search"

QA_BRANCHES = (QA_EMPTY, QA_END, QA_ESCALATE, QA_ANSWER)


def qa_classify(state: GraphState) -> str:
    """Match prod ordering exactly: empty > end > escalate > answer.
    Changing the order would change behavior (e.g. "bye" containing
    "chest pain" -> escalation vs goodbye)."""

    utterance = state.get("utterance") or ""
    skill = state["skill"]

    if not utterance.strip():
        return QA_EMPTY
    if text.wants_to_end(utterance, skill):
        return QA_END
    if text.needs_escalation(utterance, skill):
        return QA_ESCALATE
    return QA_ANSWER


# ---------------------------------------------------------------------------
# Canned-answer branches (no Bedrock cost)
# ---------------------------------------------------------------------------


def empty_input_node(state: GraphState) -> Dict[str, Any]:
    """Never invoke RAG with empty input -- incident: empty input + a
    patient blurb generated a fully personalised prep schedule the
    caller never asked for. Return a safe prompt and let Connect's
    fallback counter decide what to do."""
    skill = state["skill"]
    return {
        "branch": "qa/empty_input",
        "answer": skill.canned.empty_input_fallback,
        "close_conversation": False,
        "grounding_blocked": True,
        "retrieval_top_score": None,
    }


def end_conversation_node(state: GraphState) -> Dict[str, Any]:
    skill = state["skill"]
    return {
        "branch": "qa/end_conversation",
        "answer": skill.canned.goodbye_message,
        "close_conversation": True,
        "grounding_blocked": False,
        "retrieval_top_score": None,
    }


def escalation_node(state: GraphState) -> Dict[str, Any]:
    skill = state["skill"]
    return {
        "branch": "qa/escalation",
        "answer": skill.canned.escalation_message,
        "close_conversation": True,
        "grounding_blocked": False,
        "retrieval_top_score": None,
    }


# ---------------------------------------------------------------------------
# RAG branch
# ---------------------------------------------------------------------------


def kb_search_node(state: GraphState) -> Dict[str, Any]:
    """Stage 1 + Stage 2 RAG. Bedrock errors collapse to the language's
    bedrock_error_template (with {exc} interpolated) so the call never
    blows up; instead the caller hears a polite "couldn't reach the
    service" message and Connect's flow proceeds."""

    skill = state["skill"]
    utterance = state["utterance"]
    region = state["region"]
    caller_info = state.get("caller_info") or {}
    blurb = extractors.build_caller_info_blurb(caller_info)

    try:
        result = rag.retrieve_and_generate(
            region=region,
            user_question=utterance,
            patient_blurb=blurb,
            skill=skill,
        )
        return {
            "branch": "qa/kb_search",
            "answer": result.text,
            "close_conversation": False,
            "retrieval_top_score": result.top_score,
            "grounding_blocked": not result.grounded,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("kb_search_failed: %s", exc)
        return {
            "branch": "qa/kb_search_error",
            "answer": skill.canned.bedrock_error_template.format(exc=exc),
            "close_conversation": False,
            "retrieval_top_score": None,
            "grounding_blocked": True,
        }


def post_process_node(state: GraphState) -> Dict[str, Any]:
    """Voice-friendly cleanup. Only runs after kb_search -- the canned
    branches emit text the team already proofed for Polly, so passing it
    through ``voice_friendly`` would just re-collapse spaces."""

    return {"answer": text.voice_friendly(state["answer"])}


# ---------------------------------------------------------------------------
# Assemble the final Lex response + write audit row
# ---------------------------------------------------------------------------


def _intent_payload(state: GraphState) -> Dict[str, Any]:
    """Mirror prod: keep intent.name, set state=Fulfilled, preserve slots
    if they were present on the inbound event."""
    intent_obj = state.get("intent_obj") or {}
    payload: Dict[str, Any] = {
        "name": state["intent_name"],
        "state": "Fulfilled",
    }
    if "slots" in intent_obj and intent_obj.get("slots") is not None:
        payload["slots"] = intent_obj.get("slots") or {}
    return payload


def finalize_node(state: GraphState) -> Dict[str, Any]:
    """Build the final Lex response.

    For close-conversation branches (end / escalation) the spoken text
    is the answer verbatim. For everything else we append the skill's
    follow_up_prompt so the caller knows they can ask another question;
    the prod handler also uses ``dialogAction=Close`` (NOT ElicitIntent)
    so the matched intent survives back to Connect -- ElicitIntent
    strips ``sessionState.intent`` which breaks Connect's flow
    conditions on ``$.Lex.IntentName``."""

    skill = state["skill"]
    answer = state.get("answer", "")
    session_attributes = state.get("session_attributes_in") or {}
    intent_payload = _intent_payload(state)

    if state.get("close_conversation"):
        spoken = answer
    else:
        spoken = f"{answer} {skill.canned.follow_up_prompt}"

    response: Dict[str, Any] = {
        "sessionState": {
            "sessionAttributes": session_attributes,
            "dialogAction": {"type": "Close"},
            "intent": intent_payload,
        },
        "messages": [{"contentType": "PlainText", "content": spoken[:5000]}],
    }

    # Preserve activeContexts if the caller had any in flight.
    ss_in = (state["event"].get("sessionState") or {})
    if "activeContexts" in ss_in:
        response["sessionState"]["activeContexts"] = ss_in["activeContexts"]

    return {"spoken": spoken, "response": response}


def audit_log_node(state: GraphState) -> Dict[str, Any]:
    """Write one row to ``GIConversationTurns``. Runs only for Q&A turns
    (short-circuit branches end the graph before this node, matching
    prod where collection-intent handlers return before the logging
    call)."""

    audit.log_conversation_turn(
        session_id=state.get("session_id"),
        user_text=state.get("utterance", ""),
        bot_text=state.get("spoken", ""),
        intent_name=state["intent_name"],
        caller_phone=state.get("caller_phone"),
        patient_id=state.get("patient_id"),
        contact_id=state.get("contact_id"),
        retrieval_top_score=state.get("retrieval_top_score"),
        grounding_blocked=state.get("grounding_blocked", False),
        lang_code=state["lang_code"],
        caller_info=state.get("caller_info"),
    )
    return {}

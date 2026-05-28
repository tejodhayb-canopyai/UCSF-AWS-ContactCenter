"""FulfillmentCodeHook for the three patient-info collection intents.

Also handles the FallbackIntent-with-collectionMode-name guard path (set
up by ``setup_node`` as ``effective_intent=CollectNameIntent``). In both
cases we capture the raw transcript into a session attribute the Connect
flow reads via ``$.Lex.SessionAttributes.<key>``, then Close.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .. import text
from ..state import GraphState

logger = logging.getLogger(__name__)

# Intent name -> session attribute key the hook writes the raw transcript
# under. Connect's Save_X blocks read from these. Mirrors
# ``COLLECTION_INTENTS`` in ../../lambda_handler.py.
SESSION_ATTR_FOR_INTENT: Dict[str, str] = {
    "CollectNameIntent": "patientNameRaw",
    "CollectDateIntent": "procedureDateRaw",
    "CollectTimeIntent": "procedureTimeRaw",
}


def collection_node(state: GraphState) -> Dict[str, Any]:
    event = state["event"]
    effective_intent = state["effective_intent"]
    skill = state["skill"]

    attr_key = SESSION_ATTR_FOR_INTENT[effective_intent]
    transcript = (event.get("inputTranscript") or "").strip()

    # Empty transcript or bare DTMF "#"/"*" = explicit skip. Write the
    # language-appropriate placeholder so the confirmation playback has
    # something to read back; ``_extract_caller_info`` filters these out
    # so the skip never leaks into the Bedrock prompt.
    if (not transcript) or transcript in {"#", "*"}:
        captured = skill.skip_placeholder
        was_skip = True
    else:
        if effective_intent == "CollectNameIntent":
            captured = text.normalize_name(transcript, skill)
        else:
            captured = transcript
        was_skip = False

    session_state = event.get("sessionState") or {}
    session_attrs = dict(session_state.get("sessionAttributes") or {})
    session_attrs[attr_key] = captured

    # After capturing the name (via CollectNameIntent or the
    # FallbackIntent-name-guard re-route), clear collectionMode so it
    # cannot bleed into GI_Collect_Date / GI_Collect_Time / Q&A.
    if effective_intent == "CollectNameIntent":
        session_attrs["collectionMode"] = ""

    logger.info(
        "collection_intent_captured intent=%s lang=%s skip=%s transcript_len=%d",
        effective_intent,
        skill.language,
        was_skip,
        len(transcript),
    )

    intent_slots = (session_state.get("intent") or {}).get("slots") or {}

    # Lex requires intent.name on the response. For the fallback-name-
    # guard path the upstream intent was FallbackIntent, but we report
    # CollectNameIntent so Connect's Save_Name block sees the expected
    # IntentName and the saved attribute flows correctly. This matches
    # prod ``_handle_collection_intent(event, "CollectNameIntent", ...)``.
    response = {
        "sessionState": {
            "sessionAttributes": session_attrs,
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": effective_intent,
                "state": "Fulfilled",
                "slots": intent_slots,
            },
        },
        "messages": [],
    }
    branch = (
        "collection/fallback_guard"
        if state.get("is_fallback_name_guard")
        else f"collection/{effective_intent}"
    )
    return {"branch": branch, "response": response}

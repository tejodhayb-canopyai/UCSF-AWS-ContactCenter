"""FallbackIntent during Q&A (collectionMode != ``name``).

Connect's ``GI_Check_Fallback`` block owns the counter and the spoken
message for off-topic Q&A. We must NOT speak anything back -- doing so
would cause Polly to play two clips on the same turn (Lambda's message
+ Connect's GI_Off_Topic_Msg). So we return Close with empty
``messages`` and let Connect drive."""

from __future__ import annotations

from typing import Any, Dict

from ..state import GraphState


def fallback_close_node(state: GraphState) -> Dict[str, Any]:
    event = state["event"]
    session_attrs_in = state.get("session_attributes_in") or {}
    intent_obj = state.get("intent_obj") or {}
    slots = (intent_obj.get("slots")) or {}

    response = {
        "sessionState": {
            "sessionAttributes": dict(session_attrs_in),
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": "FallbackIntent",
                "state": "Fulfilled",
                "slots": slots,
            },
        },
        "messages": [],
    }
    return {"branch": "fallback_close", "response": response}

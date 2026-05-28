"""DialogCodeHook handler for CollectNameIntent.

This node is a faithful port of the prod ``_handle_name_dialog``. It
sets ``state["response"]`` to the complete Lex response and short-
circuits the rest of the graph -- the FulfillmentCodeHook
(``collection_node``) will run on a separate Lex invocation once the
slot is filled."""

from __future__ import annotations

import logging
from typing import Any, Dict

from ..state import GraphState

logger = logging.getLogger(__name__)


def name_dialog_node(state: GraphState) -> Dict[str, Any]:
    event = state["event"]
    lang_code = state["lang_code"]

    session_state = event.get("sessionState") or {}
    session_attrs = dict(session_state.get("sessionAttributes") or {})
    intent = session_state.get("intent") or {}
    slots = intent.get("slots") or {}

    # nameDialogRetry tracks how many spelling attempts have been made.
    # Lives in sessionAttributes (dies with the Lex session, can't
    # pollute the next call).
    retry = int(session_attrs.get("nameDialogRetry", "0"))

    name_slot = slots.get("patientName")
    slot_filled = bool(
        name_slot and (name_slot.get("value") or {}).get("interpretedValue")
    )

    if slot_filled:
        logger.info(
            "name_dialog slot=filled lang=%s retry=%d", lang_code, retry,
        )
        response = {
            "sessionState": {
                "sessionAttributes": session_attrs,
                "dialogAction": {"type": "FulfillIntent"},
                "intent": {**intent, "state": "ReadyForFulfillment"},
            },
        }
        return {"branch": "name_dialog/fulfill", "response": response}

    if retry < 1 and lang_code == "en":
        # First retry for English only: re-elicit with SpellByLetter.
        # Spanish has no slotElicitationStyle support per AWS docs.
        session_attrs["nameDialogRetry"] = "1"
        logger.info(
            "name_dialog slot=empty lang=%s retry=%d -> SpellByLetter",
            lang_code, retry,
        )
        response = {
            "sessionState": {
                "sessionAttributes": session_attrs,
                "dialogAction": {
                    "type": "ElicitSlot",
                    "slotToElicit": "patientName",
                    "slotElicitationStyle": "SpellByLetter",
                },
                "intent": {**intent, "state": "InProgress"},
            },
            "messages": [
                {
                    "contentType": "PlainText",
                    "content": (
                        "I didn't catch that. "
                        "Please spell your first name letter by letter."
                    ),
                }
            ],
        }
        return {"branch": "name_dialog/spell_retry", "response": response}

    # Either retry >= 1 OR Spanish caller: give up gracefully, hand
    # control to the FulfillmentCodeHook which writes "not provided".
    logger.info(
        "name_dialog slot=empty lang=%s retry=%d -> give up, FulfillIntent",
        lang_code, retry,
    )
    response = {
        "sessionState": {
            "sessionAttributes": session_attrs,
            "dialogAction": {"type": "FulfillIntent"},
            "intent": {**intent, "state": "ReadyForFulfillment"},
        },
    }
    return {"branch": "name_dialog/give_up", "response": response}

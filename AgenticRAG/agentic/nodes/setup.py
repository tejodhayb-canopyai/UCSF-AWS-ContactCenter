"""Entry node: parse the raw Lex event into typed state slots that
later nodes can read without re-walking dicts."""

from __future__ import annotations

from typing import Any, Dict

from .. import extractors, settings, skills_loader
from ..state import GraphState

# Intent names that the Connect flow uses to collect patient info.
# Mirrors ``COLLECTION_INTENTS`` in ../../lambda_handler.py.
COLLECTION_INTENTS = ("CollectNameIntent", "CollectDateIntent", "CollectTimeIntent")


def setup_node(state: GraphState) -> Dict[str, Any]:
    """Hydrate state from the raw Lex event.

    Also resolves the "effective" intent: if Lex misclassified a bare
    name as ``FallbackIntent`` while the Connect flow's
    ``collectionMode=name`` flag is set, we re-route through the
    name-collection path (Solution 2 in prod). This consolidates the
    decision so the router (a conditional edge) stays a pure switch."""

    event: Dict[str, Any] = state["event"]
    intent_obj: Dict[str, Any] = (event.get("sessionState") or {}).get("intent") or {}
    intent_name = intent_obj.get("name") or "FallbackIntent"

    lang_code = extractors.extract_lang_code(event)
    skill = skills_loader.get_skill(lang_code)
    session_attributes_in = extractors.session_attributes(event)

    invocation_source = event.get("invocationSource") or "FulfillmentCodeHook"

    effective_intent = intent_name
    is_fallback_name_guard = False
    if intent_name == "FallbackIntent":
        if (session_attributes_in.get("collectionMode") or "").strip() == "name":
            effective_intent = "CollectNameIntent"
            is_fallback_name_guard = True

    return {
        "region": settings.AWS_REGION,
        "skill": skill,
        "lang_code": lang_code,
        "invocation_source": invocation_source,
        "intent_name": intent_name,
        "intent_obj": intent_obj,
        "effective_intent": effective_intent,
        "is_fallback_name_guard": is_fallback_name_guard,
        "utterance": extractors.extract_user_utterance(event),
        "session_attributes_in": session_attributes_in,
    }

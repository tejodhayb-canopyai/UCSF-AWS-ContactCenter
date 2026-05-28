"""Router used as a LangGraph conditional edge.

Pure switch on the values ``setup_node`` already computed; this function
must NOT touch the event again so the routing logic stays auditable in
exactly one place (``setup_node`` decides; ``route_after_setup`` dispatches).
"""

from __future__ import annotations

from ..state import GraphState
from .setup import COLLECTION_INTENTS

BRANCH_NAME_DIALOG = "name_dialog"
BRANCH_COLLECTION = "collection"
BRANCH_FALLBACK_CLOSE = "fallback_close"
BRANCH_QA = "qa_context"

# Tuple advertised to LangGraph for static graph validation.
ALL_BRANCHES = (
    BRANCH_NAME_DIALOG,
    BRANCH_COLLECTION,
    BRANCH_FALLBACK_CLOSE,
    BRANCH_QA,
)


def route_after_setup(state: GraphState) -> str:
    """Return the name of the next node based on the effective intent +
    invocation source. Mirrors the top-level if/elif tree in prod
    ``lambda_handler.lambda_handler``."""

    effective = state["effective_intent"]
    invocation_source = state["invocation_source"]
    is_fallback_name_guard = state.get("is_fallback_name_guard", False)

    if effective == "CollectNameIntent":
        # FallbackIntent + collectionMode=name short-circuits straight to
        # the collection node (we never run name_dialog for that path --
        # the DialogCodeHook didn't fire, we're already in fulfillment).
        if invocation_source == "DialogCodeHook" and not is_fallback_name_guard:
            return BRANCH_NAME_DIALOG
        return BRANCH_COLLECTION

    if effective in COLLECTION_INTENTS:
        return BRANCH_COLLECTION

    if effective == "FallbackIntent":
        # FallbackIntent during Q&A (collectionMode != "name"): Connect's
        # GI_Check_Fallback block handles the counter + spoken message,
        # so we return Close with no messages.
        return BRANCH_FALLBACK_CLOSE

    return BRANCH_QA

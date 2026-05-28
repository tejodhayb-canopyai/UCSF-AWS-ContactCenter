"""Lambda entry point for ``GIHealthcareLexFulfillment_agentic``.

Cold-start work (skill loading + graph compile + boto3 client init)
happens at module import via the side-effects in ``agentic.__init__``
and ``agentic.skills_loader``. The handler itself is ~10 lines: invoke
the compiled graph and pull the final response out of state.

Behavior is intentionally identical to ``../lambda_handler.py``;
divergences should be PR-reviewable in the node files, not here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from agentic import compiled_graph

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Run one Lex turn through the StateGraph and return the Lex
    fulfillment response."""

    initial_state: Dict[str, Any] = {"event": event}

    final_state = compiled_graph.invoke(initial_state)

    response = final_state.get("response")
    if response is None:
        # Defensive fallback: should never happen if every terminal node
        # writes `response`. Log loudly so we notice in CloudWatch and
        # return a Close so Connect can recover the call.
        logger.error(
            "graph_finished_without_response branch=%s intent=%s lang=%s",
            final_state.get("branch") or "-",
            final_state.get("intent_name") or "-",
            final_state.get("lang_code") or "-",
        )
        return {
            "sessionState": {
                "sessionAttributes": (event.get("sessionState") or {}).get(
                    "sessionAttributes"
                )
                or {},
                "dialogAction": {"type": "Close"},
                "intent": {
                    "name": (event.get("sessionState") or {}).get("intent", {}).get(
                        "name"
                    )
                    or "FallbackIntent",
                    "state": "Fulfilled",
                },
            },
            "messages": [],
        }

    return response

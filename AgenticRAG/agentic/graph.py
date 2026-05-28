"""StateGraph definition.

Topology (drawn left-to-right; ``*`` marks terminal short-circuit nodes
that bypass the Q&A tail because they own their own ``response``):

::

                                +-- name_dialog *
                                |
    START -> setup -> route +-- collection *
                                |
                                +-- fallback_close *
                                |
                                +-- qa_context -> qa_classify --+-- empty_input --+
                                                                |                 |
                                                                +-- end_conv -----+
                                                                |                 +-> finalize -> audit_log -> END
                                                                +-- escalation ---+
                                                                |                 |
                                                                +-- kb_search ----+
                                                                       |
                                                                       v
                                                                  post_process

The compiled graph is built once at module import (cold start in
Lambda) and reused across warm invocations.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import collection, fallback_close, name_dialog, qa, router, setup
from .state import GraphState


def build_graph() -> StateGraph:
    """Construct (but do not compile) the StateGraph. Exposed separately
    so tests can introspect or swap nodes before compiling."""

    graph: StateGraph = StateGraph(GraphState)

    graph.add_node("setup", setup.setup_node)
    graph.add_node(router.BRANCH_NAME_DIALOG, name_dialog.name_dialog_node)
    graph.add_node(router.BRANCH_COLLECTION, collection.collection_node)
    graph.add_node(router.BRANCH_FALLBACK_CLOSE, fallback_close.fallback_close_node)

    graph.add_node(router.BRANCH_QA, qa.qa_context_node)
    graph.add_node(qa.QA_EMPTY, qa.empty_input_node)
    graph.add_node(qa.QA_END, qa.end_conversation_node)
    graph.add_node(qa.QA_ESCALATE, qa.escalation_node)
    graph.add_node(qa.QA_ANSWER, qa.kb_search_node)
    graph.add_node("post_process", qa.post_process_node)
    graph.add_node("finalize", qa.finalize_node)
    graph.add_node("audit_log", qa.audit_log_node)

    # Entry
    graph.add_edge(START, "setup")

    # First branch: short-circuit vs Q&A pipeline. The conditional edge
    # signature {label: node_name} keeps the dispatch table flat and
    # auditable.
    graph.add_conditional_edges(
        "setup",
        router.route_after_setup,
        {
            router.BRANCH_NAME_DIALOG: router.BRANCH_NAME_DIALOG,
            router.BRANCH_COLLECTION: router.BRANCH_COLLECTION,
            router.BRANCH_FALLBACK_CLOSE: router.BRANCH_FALLBACK_CLOSE,
            router.BRANCH_QA: router.BRANCH_QA,
        },
    )

    # Short-circuit nodes write `response` themselves and terminate.
    graph.add_edge(router.BRANCH_NAME_DIALOG, END)
    graph.add_edge(router.BRANCH_COLLECTION, END)
    graph.add_edge(router.BRANCH_FALLBACK_CLOSE, END)

    # Q&A pipeline: context extraction -> classify -> one of four nodes.
    graph.add_edge(router.BRANCH_QA, "qa_classify_router")
    # Pass-through "router" node: LangGraph requires conditional edges
    # to dispatch from a regular node, so we use a no-op node whose only
    # job is to call qa.qa_classify via the conditional-edge function.
    graph.add_node("qa_classify_router", lambda _state: {})
    graph.add_conditional_edges(
        "qa_classify_router",
        qa.qa_classify,
        {
            qa.QA_EMPTY: qa.QA_EMPTY,
            qa.QA_END: qa.QA_END,
            qa.QA_ESCALATE: qa.QA_ESCALATE,
            qa.QA_ANSWER: qa.QA_ANSWER,
        },
    )

    # Canned branches go straight to finalize.
    graph.add_edge(qa.QA_EMPTY, "finalize")
    graph.add_edge(qa.QA_END, "finalize")
    graph.add_edge(qa.QA_ESCALATE, "finalize")

    # RAG branch detours through post_process before joining finalize.
    graph.add_edge(qa.QA_ANSWER, "post_process")
    graph.add_edge("post_process", "finalize")

    # Common Q&A tail.
    graph.add_edge("finalize", "audit_log")
    graph.add_edge("audit_log", END)

    return graph


# Compile once at import time. Lambda re-uses this across warm
# invocations; cold start pays the (sub-millisecond) compile cost once.
compiled_graph = build_graph().compile()

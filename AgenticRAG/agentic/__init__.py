"""LangGraph-based fulfillment for ``GIHealthcareLexFulfillment_agentic``.

This package wraps the production ``lambda_handler.py`` direct-RAG flow in
an explicit ``StateGraph`` so each step (safety gate, RAG, post-process,
audit) is independently testable, and so adding a new language is one
markdown file in ``skills/`` instead of an inline-dict edit. Behavior is
intentionally byte-for-byte identical to prod -- the win here is
structure, not semantics.
"""

from .graph import build_graph, compiled_graph

__all__ = ["build_graph", "compiled_graph"]

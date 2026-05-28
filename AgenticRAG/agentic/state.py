"""TypedDict that describes the LangGraph state.

Every node returns a partial state dict; LangGraph merges them into the
running ``GraphState``. Using ``total=False`` lets nodes return only the
keys they touch instead of repeating the full state shape.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, TypedDict

# Forward reference so this file doesn't need a runtime dep on ``skills``
# (which the loader imports). The actual ``Skill`` type is set by the
# setup node at runtime.
SkillType = Any  # noqa: ANN401  -- intentionally Any to avoid circular import


class GraphState(TypedDict, total=False):
    """Per-invocation state flowing through the StateGraph.

    Lifecycle (every key is set by exactly one node, then read-only):

    1. ``setup_node`` populates: event, region, skill, lang_code,
       invocation_source, intent_name, intent_obj, effective_intent,
       is_fallback_name_guard, utterance, session_attributes_in.

    2. The router (a conditional edge) reads ``effective_intent`` /
       ``invocation_source`` / ``is_fallback_name_guard`` to pick a branch.

    3. Short-circuit branches (name_dialog, collection, fallback_close) set
       ``response`` directly and bypass the rest of the graph.

    4. Q&A branches populate: caller_phone, contact_id, patient_id,
       caller_info, session_id, branch, answer, close_conversation,
       retrieval_top_score, grounding_blocked.

    5. ``finalize_node`` reads the Q&A state and writes ``response`` +
       ``spoken``.

    6. ``audit_log_node`` reads everything; produces no new state.
    """

    # --- Inputs (set by setup_node) ---
    event: Dict[str, Any]
    region: str
    skill: SkillType
    lang_code: str
    invocation_source: str
    intent_name: str
    intent_obj: Dict[str, Any]
    effective_intent: str
    is_fallback_name_guard: bool
    utterance: str
    session_attributes_in: Dict[str, str]

    # --- Q&A context (set by qa_context_node) ---
    caller_phone: Optional[str]
    contact_id: Optional[str]
    patient_id: Optional[str]
    caller_info: Dict[str, str]
    session_id: Optional[str]

    # --- Branch decision recorded for logging / debugging ---
    branch: str

    # --- Q&A answer state (set by canned-answer nodes or kb_search) ---
    answer: str
    close_conversation: bool
    retrieval_top_score: Optional[float]
    grounding_blocked: bool

    # --- Final output (set by finalize_node) ---
    spoken: str
    response: Dict[str, Any]

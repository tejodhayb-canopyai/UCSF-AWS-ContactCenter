"""Bedrock Knowledge Base interactions.

Two-stage RAG, identical to prod ``_retrieve_relevant_chunks`` +
``_retrieve_and_generate``: explicit ``retrieve`` call enforces the
similarity-score grounding gate before we let the model generate (so
off-topic / misheard questions never hallucinate). The skill supplies
the per-language ``textPromptTemplate``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, NamedTuple

from skills import Skill

from . import aws_clients, settings

logger = logging.getLogger(__name__)


class RAGResult(NamedTuple):
    """Result of one knowledge-base call. ``grounded`` is True only when
    the answer came from the model with KB content; False when the
    fallback fired (gate blocked / empty output / NO_ANSWER token)."""

    text: str
    top_score: float
    grounded: bool


def retrieve_relevant_chunks(user_question: str) -> List[Dict[str, Any]]:
    """Stage 1: pure retrieval against the KB. Returns raw retrieval
    results; the gate logic lives in :func:`retrieve_and_generate`."""

    resp = aws_clients.bedrock_agent.retrieve(
        knowledgeBaseId=settings.KB_ID,
        retrievalQuery={"text": user_question},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": settings.RETRIEVAL_TOP_K},
        },
    )
    results = resp.get("retrievalResults") or []
    scores = [round(float(r.get("score") or 0.0), 4) for r in results]
    logger.info(
        "kb_retrieve query=%r top_k=%d min_score=%s scores=%s",
        user_question,
        settings.RETRIEVAL_TOP_K,
        settings.RETRIEVAL_MIN_SCORE,
        scores,
    )
    return results


def retrieve_and_generate(
    *,
    region: str,
    user_question: str,
    patient_blurb: str,
    skill: Skill,
) -> RAGResult:
    """Two-stage RAG. Stage 1 enforces the grounding gate; Stage 2 calls
    ``bedrock_agent.retrieve_and_generate`` with the skill's prompt
    template. Patient context is appended ONLY in Stage 2 so it cannot
    pollute the gate's similarity scores."""

    no_answer_fallback = skill.canned.no_answer_fallback

    if not settings.KB_ID or (not settings.MODEL_ID and not settings.MODEL_ARN_OVERRIDE):
        return RAGResult(
            text=(
                "Configuration error: set KNOWLEDGE_BASE_ID and MODEL_ID "
                "(or MODEL_ARN) on the Lambda function."
            ),
            top_score=0.0,
            grounded=False,
        )

    clean_question = user_question.strip()
    top_score = 0.0

    if settings.STRICT_GROUNDING:
        try:
            chunks = retrieve_relevant_chunks(clean_question)
        except Exception as exc:  # noqa: BLE001
            logger.exception("kb_retrieve_failed: %s", exc)
            chunks = []
        top_score = max((float(c.get("score") or 0.0) for c in chunks), default=0.0)
        if top_score < settings.RETRIEVAL_MIN_SCORE:
            logger.info(
                "grounding_gate_blocked top_score=%.4f threshold=%.4f lang=%s",
                top_score,
                settings.RETRIEVAL_MIN_SCORE,
                skill.language,
            )
            return RAGResult(text=no_answer_fallback, top_score=top_score, grounded=False)

    augmented = clean_question
    if patient_blurb:
        augmented = f"{patient_blurb}\n\nPatient question:\n{clean_question}"

    resp = aws_clients.bedrock_agent.retrieve_and_generate(
        input={"text": augmented},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": settings.KB_ID,
                "modelArn": settings.model_arn(region, settings.MODEL_ID or "placeholder"),
                "generationConfiguration": {
                    "promptTemplate": {
                        "textPromptTemplate": skill.prompt_template,
                    },
                },
            },
        },
    )

    out = ((resp.get("output") or {}).get("text") or "").strip()

    if not out:
        logger.info("model_returned_empty_output lang=%s", skill.language)
        return RAGResult(text=no_answer_fallback, top_score=top_score, grounded=False)
    if settings.NO_ANSWER_TOKEN in out.upper():
        logger.info("model_emitted_no_answer_token lang=%s", skill.language)
        return RAGResult(text=no_answer_fallback, top_score=top_score, grounded=False)

    return RAGResult(text=out, top_score=top_score, grounded=True)

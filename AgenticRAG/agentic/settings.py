"""Centralized environment-variable parsing.

Reading env vars exactly once per module load keeps cold-start cost down
and makes per-test overriding trivial (patch ``agentic.settings.KB_ID``
rather than juggling ``os.environ`` across threads).
"""

from __future__ import annotations

import os

# Knowledge base + model wiring -----------------------------------------------

KB_ID: str = os.environ.get("KNOWLEDGE_BASE_ID", "").strip()
MODEL_ID: str = os.environ.get("MODEL_ID", "").strip()
# Optional full ARN if your region requires an inference profile or a
# non-default ARN format (e.g. cross-region inference for Nova-Lite).
MODEL_ARN_OVERRIDE: str = os.environ.get("MODEL_ARN", "").strip()

# DynamoDB tables -------------------------------------------------------------

# Legacy patient-context table. Currently intentionally unset in prod
# (see ``_get_patient_context`` docstring in ../../lambda_handler.py).
PATIENT_TABLE_NAME: str = os.environ.get("PATIENT_TABLE_NAME", "").strip()
# Audit-of-record for every Q&A turn. Unset = best-effort skip (handy for
# local tests where there's no DDB).
CONVERSATION_TABLE_NAME: str = os.environ.get("CONVERSATION_TABLE_NAME", "").strip()

# Two-stage RAG grounding gate ------------------------------------------------

STRICT_GROUNDING: bool = os.environ.get("STRICT_GROUNDING", "true").lower() in (
    "1",
    "true",
    "yes",
)
RETRIEVAL_TOP_K: int = int(os.environ.get("RETRIEVAL_TOP_K", "5"))
# Default 0.38 was chosen empirically against this KB (Titan v2 embeddings):
# legit GI-prep questions land at top score >= 0.43, off-topic peaks at ~0.375.
# Tune via the RETRIEVAL_MIN_SCORE env var without redeploying code.
RETRIEVAL_MIN_SCORE: float = float(os.environ.get("RETRIEVAL_MIN_SCORE", "0.38"))

# Voice-response shaping -------------------------------------------------------

VOICE_MAX_CHARS: int = int(os.environ.get("VOICE_MAX_CHARS", "650"))

# Sentinel the model returns when the KB has no answer. Same token in both
# languages so the same detection path works regardless of locale.
NO_ANSWER_TOKEN: str = "NO_ANSWER_FOUND"

# Region resolution ------------------------------------------------------------

AWS_REGION: str = os.environ.get("AWS_REGION") or "us-east-1"


def model_arn(region: str, model_id: str) -> str:
    """Build the Bedrock foundation-model ARN (or return the override if set)."""
    if MODEL_ARN_OVERRIDE:
        return MODEL_ARN_OVERRIDE
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"

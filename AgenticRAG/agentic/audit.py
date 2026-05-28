"""DynamoDB conversation-turn logging.

Best-effort: any failure is swallowed and logged so a logging glitch can
never drop the caller's call. PHI handling matches prod -- full names go
to DDB (KMS-encrypted at rest), CloudWatch only sees the redacted form.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from . import aws_clients, settings
from .text import redact_name

logger = logging.getLogger(__name__)


def log_conversation_turn(
    *,
    session_id: Optional[str],
    user_text: str,
    bot_text: str,
    intent_name: str,
    caller_phone: Optional[str],
    patient_id: Optional[str],
    contact_id: Optional[str],
    retrieval_top_score: Optional[float],
    grounding_blocked: bool,
    lang_code: str = "en",
    caller_info: Optional[Dict[str, str]] = None,
) -> None:
    """Write one turn to ``GIConversationTurns``. Never raises."""

    if not settings.CONVERSATION_TABLE_NAME:
        return
    if not session_id:
        logger.info("conversation_log_skipped reason=missing_session_id")
        return
    info = caller_info or {}
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        turn_id = f"{now}-{uuid.uuid4()}"
        item: Dict[str, Any] = {
            "sessionId": session_id,
            "turnId": turn_id,
            "createdAt": now,
            "intent": intent_name or "",
            "userText": (user_text or "")[:4000],
            "botText": (bot_text or "")[:4000],
            "groundingBlocked": bool(grounding_blocked),
            "langCode": lang_code or "en",
        }
        if caller_phone:
            item["callerPhone"] = caller_phone
        if patient_id:
            item["patientId"] = patient_id
        if contact_id:
            item["contactId"] = contact_id
        if retrieval_top_score is not None:
            item["retrievalTopScore"] = Decimal(str(round(float(retrieval_top_score), 4)))
        for k in ("patientName", "procedureDate", "procedureTime"):
            if info.get(k):
                item[k] = info[k]
        aws_clients.ddb.Table(settings.CONVERSATION_TABLE_NAME).put_item(Item=item)
        logger.info(
            "conversation_logged session=%s turn=%s phone=%s intent=%s "
            "score=%s blocked=%s lang=%s name=%s date=%s time=%s",
            session_id,
            turn_id,
            caller_phone or "-",
            intent_name,
            retrieval_top_score if retrieval_top_score is not None else "-",
            grounding_blocked,
            lang_code or "-",
            redact_name(info.get("patientName")),
            info.get("procedureDate") or "-",
            info.get("procedureTime") or "-",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("conversation_log_failed: %s", exc)

"""
Lex V2 fulfillment for Connect inbound flow -- LexV2 + Amazon Translate edition.

English-only application core. Amazon Translate bridges any non-English caller
input/output, so:

  * All bot strings (escalation message, goodbye, follow-up prompt, etc.) are
    authored once in English.
  * The Bedrock Knowledge Base prompt template is English-only.
  * Escalation and goodbye keyword matching runs against English text.

Adding a new language requires no code changes beyond enabling the locale in
Lex (and adding the language to SUPPORTED_LANGUAGES below).

Per-turn flow for a Spanish caller:

  Connect (audio in)
    -> Lex es_US             (ASR + NLU produce Spanish text)
      -> Lambda
           Translate ES->EN    (utterance into the application core)
           Bedrock KB retrieve  (grounding gate)
           Bedrock KB generate  (English prompt -> English answer)
           Translate EN->ES    (answer back to caller)
      <- Lambda
    <- Lex                    (Spanish text)
  Connect (Polly Lupe, Spanish audio out)

English callers skip both Translate calls entirely (source == target short-circuit).

Patient-info collection intents (name, date, time) do NOT route through Translate:
proper names and Lex normalised slot values (e.g. "2026-05-22") are language-
neutral and pass straight through.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class RAGResult(NamedTuple):
    """Result of one knowledge-base call. `grounded` is True only when the
    answer came from the model with KB content; False when the fallback fired."""

    text: str
    top_score: float
    grounded: bool


VOICE_MAX_CHARS = int(os.environ.get("VOICE_MAX_CHARS", "650"))

# Sentinel the model must return when the knowledge base does not contain a clear
# answer. Unique uppercase token so it cannot collide with a real answer.
NO_ANSWER_TOKEN = "NO_ANSWER_FOUND"

# Languages this bot supports end-to-end. The values are AWS Translate language
# codes (https://docs.aws.amazon.com/translate/latest/dg/what-is-languages.html).
# Adding a new language: extend this set, add the matching locale to the Lex
# bot, and wire a language branch in the Connect flow. No application code
# changes required.
SUPPORTED_LANGUAGES = {"en", "es"}
DEFAULT_LANGUAGE = "en"

# Canned strings the bot speaks at fixed points in the conversation. Authored
# once in English; non-English versions are produced via Amazon Translate and
# cached per Lambda container (see _canned()).
CANNED_STRINGS: Dict[str, str] = {
    "follow_up_prompt": "What else can I help with?",
    "no_answer_fallback": (
        "I could not find a clear answer for that in the approved prep documents. "
        "Please rephrase your GI prep question."
    ),
    "empty_input_fallback": "I didn't catch that. Please ask your GI prep question.",
    "goodbye_message": "Thank you for calling. Goodbye.",
    "escalation_message": (
        "For your safety, I am not able to handle emergency symptoms here. "
        "Please hold while we connect you to clinical staff, or if this is an "
        "emergency, hang up and call your local emergency number."
    ),
    "bedrock_error_template": (
        "I could not reach the medical knowledge service right now."
    ),
}

# English-only safety keyword lists. Every non-English utterance is translated
# to English first, then matched. Keeping the lists in one language makes
# clinical-review of the safety triggers tractable.
ESCALATION_KEYWORDS = (
    "chest pain",
    "can't breathe",
    "cannot breathe",
    "bleeding heavily",
    "passed out",
    "fainted",
    "severe pain",
    "suicide",
    "kill myself",
)

END_CONVERSATION_KEYWORDS = (
    "bye",
    "goodbye",
    "that's all",
    "that is all",
    "no thanks",
    "no thank you",
    "i'm done",
    "im done",
)

# English-only Bedrock KB prompt. The model never sees Spanish text -- Translate
# handles the language hop on both sides of the LLM call.
PROMPT_TEMPLATE = """You are Lucy, a UCSF GI prep voice assistant. You help patients prepare for colonoscopy and other GI procedures.

Answer the patient question using ONLY the information in the search results below.

Rules:
1. Reply in 2 to 4 sentences suitable for a phone call. Include the specific actionable detail a patient needs (such as timing, amounts, what to do, and what to avoid). Do not pad with filler if the answer is genuinely brief.
2. Speak directly to the patient using "you".
3. Do NOT say "the model", "the search results", "the documents", "based on", or "according to".
4. Do NOT use outside knowledge and do NOT invent medical advice.
5. Do NOT output tool calls, action steps, JSON, function syntax, or chain-of-thought.
6. If the search results do not contain a clear answer to the patient question, respond with EXACTLY the following single token and nothing else: NO_ANSWER_FOUND

Search results:
$search_results$

Patient question:
$query$

Patient-friendly answer:"""


bedrock_agent = boto3.client("bedrock-agent-runtime")
translate = boto3.client("translate")
ddb = boto3.resource("dynamodb")

KB_ID = os.environ.get("KNOWLEDGE_BASE_ID", "").strip()
MODEL_ID = os.environ.get("MODEL_ID", "").strip()
MODEL_ARN_OVERRIDE = os.environ.get("MODEL_ARN", "").strip()
TABLE_NAME = os.environ.get("PATIENT_TABLE_NAME", "").strip()
CONVERSATION_TABLE_NAME = os.environ.get("CONVERSATION_TABLE_NAME", "").strip()

STRICT_GROUNDING = os.environ.get("STRICT_GROUNDING", "true").lower() in ("1", "true", "yes")
RETRIEVAL_TOP_K = int(os.environ.get("RETRIEVAL_TOP_K", "5"))
RETRIEVAL_MIN_SCORE = float(os.environ.get("RETRIEVAL_MIN_SCORE", "0.38"))


# ---------------------------------------------------------------------------
# Amazon Translate helpers
# ---------------------------------------------------------------------------

# Per-container cache. Keyed by (source_lang, target_lang, text). Survives
# warm invocations only -- a cold start re-pays the translation cost on first
# use, which for canned strings is at most one translate-call-per-string-per-
# language-per-container. Voice answers are too varied to cache usefully, so
# we cache the canned strings (which dominate volume) and let dynamic
# RAG answers translate fresh each turn.
_TRANSLATION_CACHE: Dict[tuple, str] = {}

# Pre-translated canned strings cache, lazily populated per language.
# _canned("follow_up_prompt", "es") returns the Spanish text and caches it.
_CANNED_CACHE: Dict[tuple, str] = {}


def _translate(text: str, source_lang: str, target_lang: str) -> str:
    """Translate `text` from source_lang to target_lang using Amazon Translate.

    Short-circuits when source == target (and when text is empty / whitespace),
    so English callers never make a Translate API call. Failures are logged
    and the original text is returned -- the caller still gets the message,
    just in the wrong language; better than a hard error mid-call.
    """
    if not text or source_lang == target_lang:
        return text
    key = (source_lang, target_lang, text)
    cached = _TRANSLATION_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        resp = translate.translate_text(
            Text=text,
            SourceLanguageCode=source_lang,
            TargetLanguageCode=target_lang,
        )
        translated = resp.get("TranslatedText") or text
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "translate_failed source=%s target=%s len=%d err=%s",
            source_lang, target_lang, len(text), exc,
        )
        return text
    _TRANSLATION_CACHE[key] = translated
    return translated


def _canned(key: str, lang_code: str) -> str:
    """Return a canned bot string in the caller's language. Translates lazily
    and caches per container so a given (key, lang) only ever calls Translate
    once per Lambda container lifetime."""
    english = CANNED_STRINGS[key]
    if lang_code == DEFAULT_LANGUAGE:
        return english
    cache_key = (key, lang_code)
    cached = _CANNED_CACHE.get(cache_key)
    if cached is not None:
        return cached
    translated = _translate(english, DEFAULT_LANGUAGE, lang_code)
    _CANNED_CACHE[cache_key] = translated
    return translated


def _resolve_language(event: Dict[str, Any]) -> str:
    """Caller's language is supplied by the Connect flow as a Lex session
    attribute (`langCode`). Unknown values fall back to English so a
    misconfigured flow can never leave the caller without a usable bot.
    Accepts both short ('es') and locale-style ('es_US' / 'es-MX') values."""
    attrs = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    raw = (attrs.get("langCode") or attrs.get("LangCode") or "").strip().lower()
    if not raw:
        return DEFAULT_LANGUAGE
    short = raw.split("_", 1)[0].split("-", 1)[0]
    return short if short in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


# ---------------------------------------------------------------------------
# RAG (English-only)
# ---------------------------------------------------------------------------

def _model_arn(region: str, model_id: str) -> str:
    if MODEL_ARN_OVERRIDE:
        return MODEL_ARN_OVERRIDE
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


def _needs_escalation_english(text_en: str) -> bool:
    t = text_en.lower()
    return any(k in t for k in ESCALATION_KEYWORDS)


def _wants_to_end_english(text_en: str) -> bool:
    t = text_en.lower().strip()
    return any(k in t for k in END_CONVERSATION_KEYWORDS)


def _voice_friendly(text: str) -> str:
    cleaned = " ".join(text.split())
    cleaned = re.sub(r"^(answer|response):\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^based on (the )?(search results|provided information),?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^according to (the )?(search results|provided documents),?\s*", "", cleaned, flags=re.I)

    if len(cleaned) <= VOICE_MAX_CHARS:
        return cleaned

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    selected: list[str] = []
    total = 0
    for sentence in sentences:
        if not sentence:
            continue
        next_total = total + len(sentence) + (1 if selected else 0)
        if selected and next_total > VOICE_MAX_CHARS:
            break
        selected.append(sentence)
        total = next_total
        if len(selected) >= 3:
            break

    shortened = " ".join(selected).strip()
    if shortened:
        return shortened
    return cleaned[: VOICE_MAX_CHARS - 1].rstrip(" ,;:") + "."


# ---------------------------------------------------------------------------
# Patient info (Step 3 collection intents)
# ---------------------------------------------------------------------------

_SKIP_PLACEHOLDERS = {
    "not provided",
    "no proporcionado",
    "no proporcionada",
}

# Connect flow seeds these per language branch via static contact attributes,
# so we never call Translate for the skip placeholder either. The strings
# match what's already in the Connect flow's GI_Set_Attrs_EN / GI_Set_Attrs_ES.
_SKIP_PLACEHOLDERS_BY_LANG: Dict[str, str] = {
    "en": "not provided",
    "es": "no proporcionado",
}

COLLECTION_INTENTS: Dict[str, str] = {
    "CollectNameIntent": "patientNameRaw",
    "CollectDateIntent": "procedureDateRaw",
    "CollectTimeIntent": "procedureTimeRaw",
}

# Both English and Spanish caller phrasing -- callers will speak in their own
# language during name collection, but the prefix-strip happens before any
# Translate call (names should not be translated). Combined regex covers both.
_NAME_PREFIX_RE = re.compile(
    r"^(?:"
    r"my name is|the name is|i\s*am|i'?m|this is|call me"
    r"|me\s+llamo|mi\s+nombre\s+es|yo\s+soy|soy|ll[aá]mame"
    r")\s+",
    re.IGNORECASE,
)


def _normalize_name(transcript: str) -> str:
    """Trim conversational prefixes from a name utterance (English or Spanish)
    so the confirmation playback and the Bedrock prompt see just the name."""
    stripped = _NAME_PREFIX_RE.sub("", transcript, count=1).strip()
    return stripped or transcript


def _handle_name_dialog(event: Dict[str, Any], lang_code: str) -> Dict[str, Any]:
    """DialogCodeHook for CollectNameIntent. Handles slot-filled passthrough
    and the one-shot SpellByLetter retry for English callers. Spanish callers
    skip the retry because spelling styles are English-only per AWS docs."""
    session_state = event.get("sessionState") or {}
    session_attrs = dict(session_state.get("sessionAttributes") or {})
    intent = session_state.get("intent") or {}
    slots = intent.get("slots") or {}

    retry = int(session_attrs.get("nameDialogRetry", "0"))
    name_slot = slots.get("patientName")
    slot_filled = bool(
        name_slot and (name_slot.get("value") or {}).get("interpretedValue")
    )

    if slot_filled:
        logger.info("name_dialog slot=filled lang=%s retry=%d", lang_code, retry)
        return {
            "sessionState": {
                "sessionAttributes": session_attrs,
                "dialogAction": {"type": "FulfillIntent"},
                "intent": {**intent, "state": "ReadyForFulfillment"},
            },
        }

    if retry < 1 and lang_code == "en":
        session_attrs["nameDialogRetry"] = "1"
        logger.info(
            "name_dialog slot=empty lang=%s retry=%d -> SpellByLetter",
            lang_code, retry,
        )
        return {
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

    logger.info(
        "name_dialog slot=empty lang=%s retry=%d -> give up, FulfillIntent",
        lang_code, retry,
    )
    return {
        "sessionState": {
            "sessionAttributes": session_attrs,
            "dialogAction": {"type": "FulfillIntent"},
            "intent": {**intent, "state": "ReadyForFulfillment"},
        },
    }


def _handle_collection_intent(
    event: Dict[str, Any],
    intent_name: str,
    lang_code: str,
) -> Dict[str, Any]:
    """Fulfillment hook for the three patient-info collection intents.

    Captures the caller's raw transcript into a session attribute Connect
    reads back via $.Lex.SessionAttributes.<key>. No Translate call: proper
    names are language-neutral and date/time values are either already
    normalised by Lex (e.g. "2026-05-22") or kept as-spoken for display.
    """
    attr_key = COLLECTION_INTENTS[intent_name]
    transcript = (event.get("inputTranscript") or "").strip()

    if (not transcript) or transcript in {"#", "*"}:
        captured = _SKIP_PLACEHOLDERS_BY_LANG.get(
            lang_code, _SKIP_PLACEHOLDERS_BY_LANG["en"]
        )
        was_skip = True
    else:
        if intent_name == "CollectNameIntent":
            captured = _normalize_name(transcript)
        else:
            captured = transcript
        was_skip = False

    session_state = event.get("sessionState") or {}
    session_attrs = dict(session_state.get("sessionAttributes") or {})
    session_attrs[attr_key] = captured

    if intent_name == "CollectNameIntent":
        session_attrs["collectionMode"] = ""

    logger.info(
        "collection_intent_captured intent=%s lang=%s skip=%s transcript_len=%d",
        intent_name, lang_code or "-", was_skip, len(transcript),
    )

    intent_slots = (session_state.get("intent") or {}).get("slots") or {}
    return {
        "sessionState": {
            "sessionAttributes": session_attrs,
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": intent_name,
                "state": "Fulfilled",
                "slots": intent_slots,
            },
        },
        "messages": [],
    }


def _extract_caller_info(event: Dict[str, Any]) -> Dict[str, str]:
    """Pull per-call patient context from session attributes. Skips the
    language-specific "not provided" placeholders so a skipped slot never
    leaks into the Bedrock prompt as if it were a real value."""
    attrs = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    info: Dict[str, str] = {}

    name_val = (attrs.get("patientName") or "").strip()
    if name_val and name_val.lower() not in _SKIP_PLACEHOLDERS:
        info["patientName"] = name_val

    for slot_key, display_key in (
        ("procedureDate", "procedureDate_display"),
        ("procedureTime", "procedureTime_display"),
    ):
        normalised = (attrs.get(slot_key) or "").strip()
        if normalised and normalised.lower() not in _SKIP_PLACEHOLDERS:
            info[slot_key] = normalised
            continue
        raw = (attrs.get(display_key) or "").strip()
        if raw and raw.lower() not in _SKIP_PLACEHOLDERS:
            info[slot_key] = raw

    return info


def _build_caller_info_blurb(caller_info: Dict[str, str]) -> str:
    """Format the per-call patient context for the Bedrock generation prompt.
    Always English -- the prompt and the model are English-only in this
    architecture; the language hop is handled by Translate before/after."""
    if not caller_info:
        return ""
    lines = [
        "Caller-supplied context (apply only if the patient question is about "
        "timing or personalised scheduling):"
    ]
    if "patientName" in caller_info:
        lines.append(f"- Patient name: {caller_info['patientName']}")
    if "procedureDate" in caller_info:
        lines.append(f"- Procedure date: {caller_info['procedureDate']}")
    if "procedureTime" in caller_info:
        lines.append(f"- Procedure time: {caller_info['procedureTime']}")
    return "\n".join(lines)


def _redact_name(name: Optional[str]) -> str:
    if not name:
        return "-"
    return f"{name[0]}***"


# ---------------------------------------------------------------------------
# Bedrock KB call (English in, English out)
# ---------------------------------------------------------------------------

def _retrieve_relevant_chunks(user_question_en: str) -> List[Dict[str, Any]]:
    resp = bedrock_agent.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": user_question_en},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": RETRIEVAL_TOP_K},
        },
    )
    results = resp.get("retrievalResults") or []
    scores = [round(float(r.get("score") or 0.0), 4) for r in results]
    logger.info(
        "kb_retrieve query=%r top_k=%d min_score=%s scores=%s",
        user_question_en, RETRIEVAL_TOP_K, RETRIEVAL_MIN_SCORE, scores,
    )
    return results


def _retrieve_and_generate(
    region: str,
    user_question_en: str,
    patient_blurb_en: str,
) -> RAGResult:
    """Two-stage RAG against the English KB. Input is already English (caller
    utterance was translated before this call); output is English too."""
    if not KB_ID or (not MODEL_ID and not MODEL_ARN_OVERRIDE):
        return RAGResult(
            text=(
                "Configuration error: set KNOWLEDGE_BASE_ID and MODEL_ID "
                "(or MODEL_ARN) on the Lambda function."
            ),
            top_score=0.0, grounded=False,
        )

    clean_question = user_question_en.strip()
    top_score = 0.0

    if STRICT_GROUNDING:
        try:
            chunks = _retrieve_relevant_chunks(clean_question)
        except Exception as exc:  # noqa: BLE001
            logger.exception("kb_retrieve_failed: %s", exc)
            chunks = []
        top_score = max(
            (float(c.get("score") or 0.0) for c in chunks), default=0.0
        )
        if top_score < RETRIEVAL_MIN_SCORE:
            logger.info(
                "grounding_gate_blocked top_score=%.4f threshold=%.4f",
                top_score, RETRIEVAL_MIN_SCORE,
            )
            return RAGResult(
                text=CANNED_STRINGS["no_answer_fallback"],
                top_score=top_score, grounded=False,
            )

    augmented = clean_question
    if patient_blurb_en:
        augmented = f"{patient_blurb_en}\n\nPatient question:\n{clean_question}"

    resp = bedrock_agent.retrieve_and_generate(
        input={"text": augmented},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": KB_ID,
                "modelArn": _model_arn(region, MODEL_ID or "placeholder"),
                "generationConfiguration": {
                    "promptTemplate": {"textPromptTemplate": PROMPT_TEMPLATE},
                },
            },
        },
    )

    out = ((resp.get("output") or {}).get("text") or "").strip()

    if not out:
        logger.info("model_returned_empty_output")
        return RAGResult(
            text=CANNED_STRINGS["no_answer_fallback"],
            top_score=top_score, grounded=False,
        )
    if NO_ANSWER_TOKEN in out.upper():
        logger.info("model_emitted_no_answer_token")
        return RAGResult(
            text=CANNED_STRINGS["no_answer_fallback"],
            top_score=top_score, grounded=False,
        )

    return RAGResult(text=out, top_score=top_score, grounded=True)


# ---------------------------------------------------------------------------
# Observability helpers (unchanged from prod)
# ---------------------------------------------------------------------------

def _extract_caller_phone(event: Dict[str, Any]) -> Optional[str]:
    attrs = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    for key in ("callerPhone", "CallerPhone", "phoneNumber", "customerPhoneNumber"):
        val = attrs.get(key)
        if val:
            return str(val).strip() or None
    req_attrs = event.get("requestAttributes") or {}
    for key in ("callerPhone", "x-amz-lex:channels:platform:caller"):
        val = req_attrs.get(key)
        if val:
            return str(val).strip() or None
    return None


def _extract_contact_id(event: Dict[str, Any]) -> Optional[str]:
    attrs = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    for key in ("contactId", "ContactId", "connectContactId"):
        val = attrs.get(key)
        if val:
            return str(val).strip() or None
    req_attrs = event.get("requestAttributes") or {}
    for key in ("x-amz-lex:contact-id", "contactId"):
        val = req_attrs.get(key)
        if val:
            return str(val).strip() or None
    sid = event.get("sessionId")
    if sid:
        return str(sid).strip() or None
    return None


def _log_conversation_turn(
    *,
    session_id: Optional[str],
    user_text_native: str,
    user_text_en: str,
    bot_text_native: str,
    bot_text_en: str,
    intent_name: str,
    caller_phone: Optional[str],
    patient_id: Optional[str],
    contact_id: Optional[str],
    retrieval_top_score: Optional[float],
    grounding_blocked: bool,
    lang_code: str,
    caller_info: Optional[Dict[str, str]] = None,
) -> None:
    """Write one turn to GIConversationTurns. Logs both the native-language
    text (what Polly spoke / what the caller said) and the English-normalised
    text (what the model saw), so audit + analytics work in English while
    transcripts preserve caller experience. Never raises."""
    if not CONVERSATION_TABLE_NAME:
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
            "userText": (user_text_native or "")[:4000],
            "userTextEn": (user_text_en or "")[:4000],
            "botText": (bot_text_native or "")[:4000],
            "botTextEn": (bot_text_en or "")[:4000],
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
            item["retrievalTopScore"] = Decimal(
                str(round(float(retrieval_top_score), 4))
            )
        for k in ("patientName", "procedureDate", "procedureTime"):
            if info.get(k):
                item[k] = info[k]
        ddb.Table(CONVERSATION_TABLE_NAME).put_item(Item=item)
        logger.info(
            "conversation_logged session=%s turn=%s phone=%s intent=%s "
            "score=%s blocked=%s lang=%s name=%s date=%s time=%s",
            session_id, turn_id, caller_phone or "-", intent_name,
            retrieval_top_score if retrieval_top_score is not None else "-",
            grounding_blocked, lang_code or "-",
            _redact_name(info.get("patientName")),
            info.get("procedureDate") or "-",
            info.get("procedureTime") or "-",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("conversation_log_failed: %s", exc)


def _extract_patient_id(event: Dict[str, Any]) -> Optional[str]:
    attrs = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    pid = attrs.get("patientId") or attrs.get("PatientId")
    if pid:
        return str(pid).strip() or None
    intent = (event.get("sessionState") or {}).get("intent") or {}
    slots = intent.get("slots") or {}
    for key in ("PatientId", "patientId"):
        slot = slots.get(key)
        if slot and slot.get("value"):
            interpret = slot["value"].get("interpretedValue")
            if interpret:
                return str(interpret).strip() or None
    return None


def _extract_user_utterance(event: Dict[str, Any]) -> str:
    trans = (event.get("inputTranscript") or "").strip()
    if trans:
        return trans
    msg = (event.get("messages") or [{}])[0]
    content = (msg.get("content") or {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    return ""


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    region = os.environ.get("AWS_REGION") or "us-east-1"
    intent = (event.get("sessionState") or {}).get("intent") or {}
    intent_name = intent.get("name") or "FallbackIntent"

    lang_code = _resolve_language(event)
    invocation_source = event.get("invocationSource") or "FulfillmentCodeHook"

    # --- Step 3 collection intents ---
    # Identical behaviour to prod: DialogCodeHook for CollectNameIntent,
    # FulfillmentCodeHook for all three. No Translate calls -- proper names
    # and Lex normalised slot values are language-neutral.
    if intent_name == "CollectNameIntent":
        if invocation_source == "DialogCodeHook":
            return _handle_name_dialog(event, lang_code)
        return _handle_collection_intent(event, intent_name, lang_code)

    if intent_name in COLLECTION_INTENTS:
        return _handle_collection_intent(event, intent_name, lang_code)

    # --- FallbackIntent guard for name collection ---
    if intent_name == "FallbackIntent":
        session_attrs_fb = (
            (event.get("sessionState") or {}).get("sessionAttributes") or {}
        )
        if (session_attrs_fb.get("collectionMode") or "").strip() == "name":
            logger.info(
                "fallback_intent_name_guard lang=%s transcript_len=%d",
                lang_code, len((event.get("inputTranscript") or "")),
            )
            return _handle_collection_intent(event, "CollectNameIntent", lang_code)

        fb_state = event.get("sessionState") or {}
        return {
            "sessionState": {
                "sessionAttributes": dict(session_attrs_fb),
                "dialogAction": {"type": "Close"},
                "intent": {
                    "name": "FallbackIntent",
                    "state": "Fulfilled",
                    "slots": (fb_state.get("intent") or {}).get("slots") or {},
                },
            },
            "messages": [],
        }

    # --- Q&A path ---
    utterance_native = _extract_user_utterance(event)
    patient_id = _extract_patient_id(event)
    caller_phone = _extract_caller_phone(event)
    contact_id = _extract_contact_id(event)
    caller_info = _extract_caller_info(event)
    session_id = event.get("sessionId") or contact_id

    close_conversation = False
    retrieval_top_score: Optional[float] = None
    grounding_blocked = False
    utterance_en = utterance_native  # filled in below
    answer_en = ""

    if not utterance_native.strip():
        # Never invoke RAG with an empty input -- documented incident in prod
        # where empty input + patient blurb generated personalised answers
        # the caller never requested.
        answer_en = CANNED_STRINGS["empty_input_fallback"]
        grounding_blocked = True
    else:
        # Translate caller utterance into the application core (English).
        # No-op short-circuit when lang_code == 'en'.
        utterance_en = _translate(utterance_native, lang_code, DEFAULT_LANGUAGE)

        if _wants_to_end_english(utterance_en):
            answer_en = CANNED_STRINGS["goodbye_message"]
            close_conversation = True
        elif _needs_escalation_english(utterance_en):
            answer_en = CANNED_STRINGS["escalation_message"]
            close_conversation = True
        else:
            ctx_en = _build_caller_info_blurb(caller_info)
            try:
                rag = _retrieve_and_generate(region, utterance_en, ctx_en)
                answer_en = _voice_friendly(rag.text)
                retrieval_top_score = rag.top_score
                grounding_blocked = not rag.grounded
            except Exception as exc:  # noqa: BLE001
                answer_en = (
                    f"{CANNED_STRINGS['bedrock_error_template']} ({exc})"
                )
                grounding_blocked = True

    # Translate the answer back to the caller's language. For cached canned
    # strings (no_answer_fallback, goodbye, escalation, empty_input) this is
    # a cache hit on warm containers. RAG-generated answers always translate
    # fresh.
    answer_native = _translate(answer_en, DEFAULT_LANGUAGE, lang_code)

    session_attributes = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    intent_payload: Dict[str, Any] = {"name": intent_name, "state": "Fulfilled"}
    if "slots" in intent and intent.get("slots") is not None:
        intent_payload["slots"] = intent.get("slots") or {}

    if close_conversation:
        spoken_native = answer_native
        spoken_en = answer_en
    else:
        # Follow-up prompt: cached, so this is at most one Translate call per
        # language per Lambda container lifetime.
        follow_up_native = _canned("follow_up_prompt", lang_code)
        spoken_native = f"{answer_native} {follow_up_native}"
        spoken_en = f"{answer_en} {CANNED_STRINGS['follow_up_prompt']}"

    # Use dialogAction=Close so the matched intent survives back to Connect.
    # See prod-bot Lambda comment: ElicitIntent strips sessionState.intent
    # which breaks Connect's intent-name conditions on the next turn.
    response: Dict[str, Any] = {
        "sessionState": {
            "sessionAttributes": session_attributes,
            "dialogAction": {"type": "Close"},
            "intent": intent_payload,
        },
        "messages": [
            {"contentType": "PlainText", "content": spoken_native[:5000]}
        ],
    }

    ss = event.get("sessionState") or {}
    if "activeContexts" in ss:
        response["sessionState"]["activeContexts"] = ss["activeContexts"]

    _log_conversation_turn(
        session_id=session_id,
        user_text_native=utterance_native,
        user_text_en=utterance_en,
        bot_text_native=spoken_native,
        bot_text_en=spoken_en,
        intent_name=intent_name,
        caller_phone=caller_phone,
        patient_id=patient_id,
        contact_id=contact_id,
        retrieval_top_score=retrieval_top_score,
        grounding_blocked=grounding_blocked,
        lang_code=lang_code,
        caller_info=caller_info,
    )

    return response


# For quick local import test
if __name__ == "__main__":
    sample_path = Path(__file__).resolve().parents[2] / "events" / "lex-sample.json"
    if sample_path.exists():
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        print(json.dumps(lambda_handler(sample, None), indent=2))
    else:
        print(f"No sample event at {sample_path}")

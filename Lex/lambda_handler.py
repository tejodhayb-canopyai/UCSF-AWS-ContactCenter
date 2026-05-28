"""
Lex V2 fulfillment for Connect inbound flow.
DynamoDB (patient context) + Bedrock Knowledge Base RetrieveAndGenerate (Claude) → plain text for Lex/Polly.
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
# answer. We use a unique uppercase token so it cannot collide with a real answer
# and so the Lambda can detect it without brittle phrase matching. Same token for
# both languages so the same detection code path works regardless of locale.
NO_ANSWER_TOKEN = "NO_ANSWER_FOUND"

# All caller-visible strings and language-specific keyword lists are centralized
# here so the bot can speak the caller's preferred language end-to-end. Bot
# language is selected by the Connect flow's DTMF gate and passed to Lambda as
# the `langCode` Lex session attribute (`en` or `es`). When the attribute is
# missing or unrecognised we default to English to preserve current behavior.
LANG_STRINGS: Dict[str, Dict[str, Any]] = {
    "en": {
        "escalation_keywords": (
            "chest pain",
            "can't breathe",
            "cannot breathe",
            "bleeding heavily",
            "passed out",
            "fainted",
            "severe pain",
            "suicide",
            "kill myself",
        ),
        "end_conversation_keywords": (
            "bye",
            "goodbye",
            "that's all",
            "that is all",
            "no thanks",
            "no thank you",
            "i'm done",
            "im done",
        ),
        "follow_up_prompt": "What else can I help with?",
        "no_answer_fallback": (
            "I could not find a clear answer for that in the approved prep documents. "
            "Please rephrase your GI prep question."
        ),
        "empty_input_fallback": "I didn't catch that. Please ask your GI prep question.",
        "goodbye_message": "Thank you for calling. Goodbye.",
        "escalation_message": (
            "For your safety, I am not able to handle emergency symptoms here. "
            "Please hold while we connect you to clinical staff, or if this is an emergency, "
            "hang up and call your local emergency number."
        ),
        "bedrock_error_template": "I could not reach the medical knowledge service right now. ({exc})",
    },
    "es": {
        "escalation_keywords": (
            "dolor de pecho",
            "no puedo respirar",
            "sangrando mucho",
            "sangro mucho",
            "me desmaye",
            "me desmayé",
            "perdi el conocimiento",
            "perdí el conocimiento",
            "dolor severo",
            "dolor muy fuerte",
            "suicidio",
            "matarme",
        ),
        "end_conversation_keywords": (
            "adios",
            "adiós",
            "hasta luego",
            "eso es todo",
            "ya termine",
            "ya terminé",
            "no gracias",
            "ya estoy bien",
            "muchas gracias adios",
            "muchas gracias adiós",
        ),
        "follow_up_prompt": "¿En qué más puedo ayudarte?",
        "no_answer_fallback": (
            "No encontré una respuesta clara en los documentos de preparación aprobados. "
            "Por favor reformula tu pregunta sobre la preparación."
        ),
        "empty_input_fallback": (
            "No te escuché bien. Por favor haz tu pregunta sobre la preparación."
        ),
        "goodbye_message": "Gracias por llamar. Adiós.",
        "escalation_message": (
            "Por tu seguridad, no puedo atender síntomas de emergencia aquí. "
            "Por favor espera mientras te conecto con personal clínico, o si es una emergencia, "
            "cuelga y llama al número de emergencias local."
        ),
        "bedrock_error_template": "No pude acceder al servicio de información médica en este momento. ({exc})",
    },
}

# Custom prompt templates sent to Bedrock Knowledge Base. Bedrock substitutes
# $search_results$ with the retrieved chunks and $query$ with the input text.
# These enforce voice-friendly style and a strict no-answer contract.
#
# The Spanish prompt instructs Nova to translate the English source material at
# inference time. This is an MVP-only approach -- production deployment should
# use a clinically-translated Spanish source PDF and either a separate KB or a
# language-tagged data source, so grounding happens against native Spanish
# medical text rather than relying on the model to translate accurately.
PROMPT_TEMPLATES: Dict[str, str] = {
    "en": """You are Lucy, a UCSF GI prep voice assistant. You help patients prepare for colonoscopy and other GI procedures.

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

Patient-friendly answer:""",
    "es": """Eres Lucy, una asistente de voz de UCSF para preparación de procedimientos gastrointestinales. Ayudas a pacientes que se preparan para colonoscopia y otros procedimientos GI.

Los documentos de referencia están en inglés. Responde la pregunta del paciente usando SOLO la información de los resultados de búsqueda. Traduce tu respuesta a español natural y claro.

Reglas:
1. Responde en 2 a 4 oraciones aptas para una llamada telefónica. Incluye el detalle específico y accionable que el paciente necesita (como tiempos, cantidades, qué hacer y qué evitar). No agregues relleno si la respuesta es genuinamente breve.
2. Habla directamente al paciente usando "tú".
3. NO digas "el modelo", "los resultados de búsqueda", "los documentos", "según" o "basado en".
4. NO uses conocimiento externo y NO inventes consejos médicos.
5. NO produzcas llamadas a herramientas, pasos de acción, JSON, sintaxis de funciones ni razonamiento interno.
6. Si los resultados de búsqueda no contienen una respuesta clara a la pregunta del paciente, responde EXACTAMENTE con el siguiente token y nada más: NO_ANSWER_FOUND

Resultados de búsqueda:
$search_results$

Pregunta del paciente:
$query$

Respuesta al paciente:""",
}


def _strings(lang_code: str) -> Dict[str, Any]:
    """Return the language-specific string bundle, falling back to English for
    any unknown language code so we never break the call if the flow sends an
    unexpected value."""
    return LANG_STRINGS.get(lang_code) or LANG_STRINGS["en"]


def _prompt_template(lang_code: str) -> str:
    return PROMPT_TEMPLATES.get(lang_code) or PROMPT_TEMPLATES["en"]

bedrock_agent = boto3.client("bedrock-agent-runtime")
ddb = boto3.resource("dynamodb")

KB_ID = os.environ.get("KNOWLEDGE_BASE_ID", "").strip()
MODEL_ID = os.environ.get("MODEL_ID", "").strip()
# Optional full ARN if your region requires an inference profile or non-default ARN format.
MODEL_ARN_OVERRIDE = os.environ.get("MODEL_ARN", "").strip()
TABLE_NAME = os.environ.get("PATIENT_TABLE_NAME", "").strip()
# DynamoDB table that captures every turn for verification/audit. Optional --
# if unset, logging is silently skipped (useful for local tests).
CONVERSATION_TABLE_NAME = os.environ.get("CONVERSATION_TABLE_NAME", "").strip()

# Two-stage RAG grounding gate. We retrieve chunks first and require at least
# one chunk with a similarity score >= RETRIEVAL_MIN_SCORE before we let the
# model generate an answer. This is deterministic, observable, and independent
# of model-specific citation behavior with custom prompt templates.
STRICT_GROUNDING = os.environ.get("STRICT_GROUNDING", "true").lower() in ("1", "true", "yes")
RETRIEVAL_TOP_K = int(os.environ.get("RETRIEVAL_TOP_K", "5"))
# Default 0.38 was chosen empirically against this KB (Titan v2 embeddings):
# legit GI-prep questions land at top score >= 0.43, off-topic peaks at ~0.375.
# Tune via the RETRIEVAL_MIN_SCORE env var without redeploying code.
RETRIEVAL_MIN_SCORE = float(os.environ.get("RETRIEVAL_MIN_SCORE", "0.38"))


def _model_arn(region: str, model_id: str) -> str:
    if MODEL_ARN_OVERRIDE:
        return MODEL_ARN_OVERRIDE
    return f"arn:aws:bedrock:{region}::foundation-model/{model_id}"


def _needs_escalation(text: str, lang_code: str) -> bool:
    t = text.lower()
    return any(k in t for k in _strings(lang_code)["escalation_keywords"])


def _wants_to_end(text: str, lang_code: str) -> bool:
    t = text.lower().strip()
    return any(k in t for k in _strings(lang_code)["end_conversation_keywords"])


def _extract_lang_code(event: Dict[str, Any]) -> str:
    """Caller's language is supplied by the Connect flow as a Lex session
    attribute (`langCode`). We only recognise the locales the bot is actually
    built for ('en' and 'es'); anything else falls back to English so a
    misconfigured flow can never leave the caller without a usable bot."""
    attrs = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    raw = (attrs.get("langCode") or attrs.get("LangCode") or "").strip().lower()
    # Accept both short ('es') and locale-style ('es_US' / 'es-MX') values.
    if raw.startswith("es"):
        return "es"
    return "en"


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


def _get_patient_context(patient_id: Optional[str]) -> str:
    """Legacy DynamoDB-backed patient context lookup. Currently disabled in
    production (PATIENT_TABLE_NAME env var is intentionally unset; see PHI
    incident note in the README) but kept here so the Phase-2 work to
    verify callers against `GIPatients` doesn't have to rebuild this from
    scratch. Per-call info captured during the flow's collection step is
    handled by `_extract_caller_info` instead."""
    if not patient_id or not TABLE_NAME:
        return ""
    try:
        table = ddb.Table(TABLE_NAME)
        res = table.get_item(Key={"patientId": patient_id})
        item = res.get("Item")
        if not item:
            return f"Patient ID {patient_id} not found in records."
        # Human-readable context for the model (no PHI beyond what you store)
        parts = [f"PatientId: {patient_id}"]
        for k in ("fullName", "procedureType", "procedureDate", "prepStartTime", "prepType", "notes"):
            if k in item:
                parts.append(f"{k}: {item[k]}")
        return "Known patient context:\n" + "\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        return f"(Could not load patient context: {exc})"


# Placeholder strings the Connect flow seeds on the `_display` attributes
# AND that this Lambda writes back into session attributes when the caller
# skipped a collection step. They're normal English / Spanish words and we
# MUST NOT pass them into the model prompt as if they were the actual
# values -- callers rejected those slots intentionally.
_SKIP_PLACEHOLDERS = {
    "not provided",
    "no proporcionado",
    "no proporcionada",
}

# Language-keyed skip placeholders the collection-intent hook writes back
# when the caller is silent or presses "#". Using language-appropriate text
# means the confirmation playback ("...your procedure date as <X>...") reads
# naturally even when a slot was skipped, without needing conditional
# message blocks in the Connect flow.
_SKIP_PLACEHOLDERS_BY_LANG: Dict[str, str] = {
    "en": "not provided",
    "es": "no proporcionado",
}

# Patient-info collection intents (Step 3). Each entry maps the Lex intent
# name to the session-attribute key the hook writes the raw transcript
# under. The Connect flow's Save_X blocks read these via
# $.Lex.SessionAttributes.<key>. This indirection is necessary because
# $.Lex.InputTranscript is NOT a supported attribute path in Connect
# contact flows (only $.Lex.IntentName, $.Lex.Slots.X,
# $.Lex.SessionAttributes.X, $.Lex.SentimentResponse.*, and
# $.Lex.DialogState are exposed -- see Connect docs page
# `connect-attrib-list.html`). Without this hook we lost the caller's raw
# name utterance entirely, and the date/time confirmation playback was
# silent because the InputTranscript paths resolved to empty strings.
COLLECTION_INTENTS: Dict[str, str] = {
    "CollectNameIntent": "patientNameRaw",
    "CollectDateIntent": "procedureDateRaw",
    "CollectTimeIntent": "procedureTimeRaw",
}

# Common conversational prefixes callers put in front of their name. We
# strip these so the confirmation playback says "I have your name as
# Tejodhay" instead of "I have your name as my name is Tejodhay". Each
# regex is anchored at start of utterance and matches only the prefix
# (the actual name is preserved). One pass per language; the patterns
# mirror the sample utterances on the Collect intents.
_NAME_PREFIX_RE_EN = re.compile(
    r"^(?:my name is|the name is|i\s*am|i'?m|this is|call me)\s+",
    re.IGNORECASE,
)
_NAME_PREFIX_RE_ES = re.compile(
    r"^(?:me\s+llamo|mi\s+nombre\s+es|yo\s+soy|soy|ll[aá]mame)\s+",
    re.IGNORECASE,
)


def _normalize_name(transcript: str, lang_code: str) -> str:
    """Trim conversational prefixes from a name utterance so the
    confirmation playback and the Bedrock prompt see just the name. If
    stripping the prefix would yield an empty string we keep the original
    transcript -- something is better than nothing for confirmation."""
    pattern = _NAME_PREFIX_RE_ES if lang_code == "es" else _NAME_PREFIX_RE_EN
    stripped = pattern.sub("", transcript, count=1).strip()
    return stripped or transcript


def _handle_name_dialog(
    event: Dict[str, Any],
    lang_code: str,
) -> Dict[str, Any]:
    """DialogCodeHook (initialization/validation) handler for CollectNameIntent.

    This fires on EVERY dialog turn BEFORE Lex processes the user's next
    utterance, giving us a chance to:

      1. Accept the filled AMAZON.FirstName slot and move to fulfillment —
         the FulfillmentCodeHook (`_handle_collection_intent`) then writes
         the raw transcript to `patientNameRaw`.
      2. On the first failed attempt (slot still empty after the caller
         spoke), offer a spell-by-letter retry for English callers. This
         handles names not in the AMAZON.FirstName dictionary (e.g.
         unusual or uncommon first names) by asking the caller to spell
         out their name letter by letter.
      3. After the second attempt (or for Spanish where spell-by-letter is
         not supported per AWS docs), give up gracefully and proceed —
         the FulfillmentCodeHook will write whatever the transcript says
         (or the "not provided" placeholder if it was blank).

    Spelling styles (SpellByLetter / SpellByWord) are supported ONLY for
    English (en_US, en_GB, en_AU) — see AWS docs: Capturing slot values
    with spelling styles during the conversation. Spanish callers skip
    the retry and go straight to fulfillment after one attempt."""

    session_state = event.get("sessionState") or {}
    session_attrs = dict(session_state.get("sessionAttributes") or {})
    intent = session_state.get("intent") or {}
    slots = intent.get("slots") or {}

    # nameDialogRetry tracks how many spelling attempts have been made.
    # We write it to sessionAttributes (not contactAttributes) so it dies
    # with the Lex session and can't pollute the next call.
    retry = int(session_attrs.get("nameDialogRetry", "0"))

    # Check whether the AMAZON.FirstName slot was filled by this turn.
    name_slot = slots.get("patientName")
    slot_filled = bool(
        name_slot
        and (name_slot.get("value") or {}).get("interpretedValue")
    )

    if slot_filled:
        # Slot is filled — hand control to the FulfillmentCodeHook by
        # advancing the dialog to FulfillIntent. The fulfillment hook will
        # capture event.inputTranscript → patientNameRaw.
        logger.info(
            "name_dialog slot=filled lang=%s retry=%d",
            lang_code, retry,
        )
        return {
            "sessionState": {
                "sessionAttributes": session_attrs,
                "dialogAction": {"type": "FulfillIntent"},
                "intent": {**intent, "state": "ReadyForFulfillment"},
            },
        }

    # Slot is still empty after the caller's utterance.
    if retry < 1 and lang_code == "en":
        # First retry for English only: re-elicit with SpellByLetter.
        # Polly will play the retry prompt defined on the slot.
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

    # Either: retry >= 1, OR Spanish caller (no spelling support).
    # Give up gracefully — proceed to fulfillment with whatever the
    # transcript has (could be blank, in which case the fulfillment hook
    # writes "not provided"). The caller can press 2 at the confirmation
    # screen to re-enter.
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
    """Lex fulfillment hook for the three patient-info collection intents.

    Capture the caller's raw transcript into a session attribute that
    Connect can read back via $.Lex.SessionAttributes.<key>, then close
    the intent immediately so the flow returns control to Connect without
    any additional model calls. We never touch Bedrock here -- the only
    job is to expose the utterance text to the Connect flow."""
    attr_key = COLLECTION_INTENTS[intent_name]
    transcript = (event.get("inputTranscript") or "").strip()

    # Treat a bare DTMF "#" / "*" or an empty transcript as an explicit
    # skip. We still write a value (the language-appropriate "not provided"
    # placeholder) so the confirmation playback has something to read
    # back; _extract_caller_info filters these strings out so the skip
    # never leaks into the Bedrock prompt as if it were the caller's name.
    if (not transcript) or transcript in {"#", "*"}:
        captured = _SKIP_PLACEHOLDERS_BY_LANG.get(lang_code, _SKIP_PLACEHOLDERS_BY_LANG["en"])
        was_skip = True
    else:
        # For the name intent, strip conversational prefixes ("my name
        # is", "me llamo", ...) so the confirmation playback reads
        # cleanly. Date and time transcripts go through untouched -- the
        # caller's natural phrasing ("May 22", "nueve de la manana") is
        # already what we want Polly to read back.
        if intent_name == "CollectNameIntent":
            captured = _normalize_name(transcript, lang_code)
        else:
            captured = transcript
        was_skip = False

    session_state = event.get("sessionState") or {}
    session_attrs = dict(session_state.get("sessionAttributes") or {})
    session_attrs[attr_key] = captured

    # After capturing the name (whether via CollectNameIntent or the
    # FallbackIntent guard), clear the collectionMode flag so it cannot
    # bleed into GI_Collect_Date, GI_Collect_Time, or GI_Inbound_Main.
    if intent_name == "CollectNameIntent":
        session_attrs["collectionMode"] = ""

    logger.info(
        "collection_intent_captured intent=%s lang=%s skip=%s transcript_len=%d",
        intent_name,
        lang_code or "-",
        was_skip,
        len(transcript),
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
    """Pull the per-call patient context the Connect flow's collection step
    saved into Lex session attributes (`patientName`, `procedureDate`,
    `procedureTime`, with `procedureDate_display` / `procedureTime_display`
    as raw-transcript fallbacks when Lex's slot normalisation missed).

    Skips the language-appropriate "not provided" placeholders that the
    collection hook writes when the caller pressed "#" or stayed silent,
    so a skipped slot never leaks into the Bedrock prompt as if it were
    the real value.

    Returns a dict with only the keys we actually captured; an empty dict
    means the caller skipped everything (or this Lambda was invoked outside
    the Connect flow, e.g. via direct test). The caller is expected to feed
    this to `_build_caller_info_blurb` before handing it to Bedrock."""
    attrs = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    info: Dict[str, str] = {}

    name_val = (attrs.get("patientName") or "").strip()
    if name_val and name_val.lower() not in _SKIP_PLACEHOLDERS:
        info["patientName"] = name_val

    # Prefer the normalised slot value (e.g. "2026-05-15") so the model gets
    # an unambiguous date, but fall back to the caller's raw transcript
    # ("May 15") if Lex misclassified the date utterance and never filled
    # the slot. Skip the placeholder strings the flow uses for missing data.
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

    Kept in English regardless of caller language so the model sees a
    consistent structural cue; the prompt template already handles the
    response-language switch. The blurb is appended only to the Stage-2
    generation query, never the Stage-1 retrieval query, so it cannot
    pollute the grounding-gate similarity scores."""
    if not caller_info:
        return ""
    lines = ["Caller-supplied context (apply only if the patient question is about timing or personalised scheduling):"]
    if "patientName" in caller_info:
        lines.append(f"- Patient name: {caller_info['patientName']}")
    if "procedureDate" in caller_info:
        lines.append(f"- Procedure date: {caller_info['procedureDate']}")
    if "procedureTime" in caller_info:
        lines.append(f"- Procedure time: {caller_info['procedureTime']}")
    return "\n".join(lines)


def _redact_name(name: Optional[str]) -> str:
    """Mask a caller name for CloudWatch log output. We keep the first
    character so we can still spot patterns ("J***" recurring) without
    exposing the full PHI. The unredacted name is still written to the
    GIConversationTurns audit table (KMS-encrypted at rest)."""
    if not name:
        return "-"
    first = name[0]
    return f"{first}***"


def _retrieve_relevant_chunks(user_question: str) -> List[Dict[str, Any]]:
    """Stage 1 of two-stage RAG: pure retrieval against the KB.

    Returns the raw list of retrieval results. We log scores for observability
    so the threshold can be tuned from CloudWatch without code changes.
    """
    resp = bedrock_agent.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": user_question},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": RETRIEVAL_TOP_K},
        },
    )
    results = resp.get("retrievalResults") or []
    scores = [round(float(r.get("score") or 0.0), 4) for r in results]
    logger.info(
        "kb_retrieve query=%r top_k=%d min_score=%s scores=%s",
        user_question,
        RETRIEVAL_TOP_K,
        RETRIEVAL_MIN_SCORE,
        scores,
    )
    return results


def _retrieve_and_generate(
    region: str,
    user_question: str,
    patient_blurb: str,
    lang_code: str,
) -> RAGResult:
    strings = _strings(lang_code)
    no_answer_fallback = strings["no_answer_fallback"]

    if not KB_ID or (not MODEL_ID and not MODEL_ARN_OVERRIDE):
        return RAGResult(
            text="Configuration error: set KNOWLEDGE_BASE_ID and MODEL_ID (or MODEL_ARN) on the Lambda function.",
            top_score=0.0,
            grounded=False,
        )

    clean_question = user_question.strip()
    top_score = 0.0

    # Two-stage RAG: Stage 1 -- grounding gate via explicit retrieval.
    # We query the KB with the raw patient question (no patient blurb, since
    # that pollutes the similarity search). If no chunk clears the relevance
    # threshold we refuse to generate, which prevents hallucination on
    # off-topic or misheard questions. Titan v2 embeddings are multilingual,
    # so Spanish queries retrieve sensibly against the English source corpus
    # without any language-specific tuning here.
    if STRICT_GROUNDING:
        try:
            chunks = _retrieve_relevant_chunks(clean_question)
        except Exception as exc:  # noqa: BLE001
            logger.exception("kb_retrieve_failed: %s", exc)
            chunks = []
        top_score = max((float(c.get("score") or 0.0) for c in chunks), default=0.0)
        if top_score < RETRIEVAL_MIN_SCORE:
            logger.info(
                "grounding_gate_blocked top_score=%.4f threshold=%.4f lang=%s",
                top_score,
                RETRIEVAL_MIN_SCORE,
                lang_code,
            )
            return RAGResult(text=no_answer_fallback, top_score=top_score, grounded=False)

    # Stage 2 -- generation. Patient context is appended only here, never sent
    # to the retrieval query.
    augmented = clean_question
    if patient_blurb:
        augmented = f"{patient_blurb}\n\nPatient question:\n{clean_question}"

    resp = bedrock_agent.retrieve_and_generate(
        input={"text": augmented},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": KB_ID,
                "modelArn": _model_arn(region, MODEL_ID or "placeholder"),
                "generationConfiguration": {
                    "promptTemplate": {
                        "textPromptTemplate": _prompt_template(lang_code),
                    },
                },
            },
        },
    )

    out = ((resp.get("output") or {}).get("text") or "").strip()

    # Explicit no-answer contract enforced by the prompt template. The model is
    # instructed to return NO_ANSWER_TOKEN verbatim (same token in both
    # languages) if it cannot answer from the retrieved chunks. This is
    # defense in depth on top of the gate above.
    if not out:
        logger.info("model_returned_empty_output lang=%s", lang_code)
        return RAGResult(text=no_answer_fallback, top_score=top_score, grounded=False)
    if NO_ANSWER_TOKEN in out.upper():
        logger.info("model_emitted_no_answer_token lang=%s", lang_code)
        return RAGResult(text=no_answer_fallback, top_score=top_score, grounded=False)

    return RAGResult(text=out, top_score=top_score, grounded=True)


def _extract_caller_phone(event: Dict[str, Any]) -> Optional[str]:
    """The Connect flow forwards $.CustomerEndpoint.Address as a Lex session
    attribute named `callerPhone`. We also accept a few common alternates so
    this works under direct Lambda invocation / future flow refactors."""
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
    """When Lex is invoked from Amazon Connect, the Lex sessionId is the
    Connect Contact ID. We also check session/request attributes in case a
    future flow change pushes it explicitly."""
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
    """Write one turn to GIConversationTurns. Never raises -- a logging
    failure must not break the user's call.

    PHI handling: full caller-supplied name / date / time are stored in
    DynamoDB (the audit-of-record, KMS-encrypted at rest). CloudWatch logs
    only show a first-character mask for the name so day-to-day operational
    debugging never leaks PHI."""
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
            # DynamoDB rejects native floats; store as Decimal.
            item["retrievalTopScore"] = Decimal(str(round(float(retrieval_top_score), 4)))
        for k in ("patientName", "procedureDate", "procedureTime"):
            if info.get(k):
                item[k] = info[k]
        ddb.Table(CONVERSATION_TABLE_NAME).put_item(Item=item)
        logger.info(
            "conversation_logged session=%s turn=%s phone=%s intent=%s score=%s blocked=%s lang=%s name=%s date=%s time=%s",
            session_id,
            turn_id,
            caller_phone or "-",
            intent_name,
            retrieval_top_score if retrieval_top_score is not None else "-",
            grounding_blocked,
            lang_code or "-",
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
    # Fallback: slot on common intent names
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
    # Fallback: slot or request attribute
    msg = (event.get("messages") or [{}])[0]
    content = (msg.get("content") or {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    return ""


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    region = os.environ.get("AWS_REGION") or "us-east-1"
    intent = (event.get("sessionState") or {}).get("intent") or {}
    intent_name = intent.get("name") or "FallbackIntent"

    lang_code = _extract_lang_code(event)
    strings = _strings(lang_code)
    invocation_source = (event.get("invocationSource") or "FulfillmentCodeHook")

    # --- Step 3 collection intents ---
    # CollectNameIntent has BOTH a DialogCodeHook (fires every turn, handles
    # the two-attempt AMAZON.FirstName + SpellByLetter pattern) AND a
    # FulfillmentCodeHook (fires once after the slot is accepted, captures
    # the raw transcript into patientNameRaw).  CollectDateIntent and
    # CollectTimeIntent only have the FulfillmentCodeHook.
    if intent_name == "CollectNameIntent":
        if invocation_source == "DialogCodeHook":
            return _handle_name_dialog(event, lang_code)
        # FulfillmentCodeHook falls through to the shared handler below.
        return _handle_collection_intent(event, intent_name, lang_code)

    if intent_name in COLLECTION_INTENTS:
        # CollectDateIntent / CollectTimeIntent — fulfillment hook only.
        return _handle_collection_intent(event, intent_name, lang_code)

    # FallbackIntent guard for name collection (Solution 2).
    #
    # AMAZON.FirstName (and any custom name slot) can't prevent NLU from
    # routing bare names ("Tejodhay", "John") to FallbackIntent because
    # single-word utterances match no sample-utterance pattern with enough
    # confidence. We work around this by:
    #   1. Connect sets `collectionMode=name` in LexSessionAttributes when
    #      it starts GI_Collect_Name.
    #   2. The FallbackIntent fulfillment hook is active (Lex alias config).
    #   3. Lambda checks that flag here and redirects to _handle_collection_intent
    #      so the raw transcript lands in `patientNameRaw` exactly as if
    #      CollectNameIntent had matched.
    #   4. _handle_collection_intent clears `collectionMode` before returning so
    #      the flag cannot bleed into GI_Collect_Date, GI_Collect_Time, or the
    #      main Q&A bot block.
    #
    # For FallbackIntent outside the collection phase (Q&A), `collectionMode`
    # is absent/empty so this guard is never entered and the existing Connect
    # flow fallback counter handles the turn (no RAG, no Bedrock call).
    if intent_name == "FallbackIntent":
        session_attrs_fb = (
            (event.get("sessionState") or {}).get("sessionAttributes") or {}
        )
        if (session_attrs_fb.get("collectionMode") or "").strip() == "name":
            logger.info(
                "fallback_intent_name_guard lang=%s transcript_len=%d",
                lang_code,
                len((event.get("inputTranscript") or "")),
            )
            return _handle_collection_intent(event, "CollectNameIntent", lang_code)

        # FallbackIntent during Q&A: return Close with no messages so the
        # Connect flow's GI_Check_Fallback block handles the counter and
        # the spoken message (avoids duplicate speech to the caller).
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

    utterance = _extract_user_utterance(event)
    patient_id = _extract_patient_id(event)
    caller_phone = _extract_caller_phone(event)
    contact_id = _extract_contact_id(event)
    caller_info = _extract_caller_info(event)
    session_id = event.get("sessionId") or contact_id

    close_conversation = False
    retrieval_top_score: Optional[float] = None
    grounding_blocked = False

    if not utterance.strip():
        # Safety: never invoke RAG with an empty/placeholder query. Doing so
        # produced borderline-score retrievals that the model then "answered"
        # using any injected context (incident: empty input + patient blurb
        # generated a fully personalized prep schedule the caller never asked
        # for). Return a safe prompt instead and let the Connect flow's
        # fallback counter decide whether to retry or disconnect.
        answer = strings["empty_input_fallback"]
        grounding_blocked = True
    elif _wants_to_end(utterance, lang_code):
        answer = strings["goodbye_message"]
        close_conversation = True
    elif _needs_escalation(utterance, lang_code):
        answer = strings["escalation_message"]
        close_conversation = True
    else:
        # `patient_blurb` accepts any opt-in caller context that should ride
        # along with the question into the Bedrock generation prompt -- but
        # only Stage 2 (generation), never Stage 1 (retrieval), so it cannot
        # skew the grounding gate. Today the only source is the per-call
        # info the Connect flow collected (name + date + time); the legacy
        # DDB-backed patient lookup is intentionally disabled (see
        # `_get_patient_context` docstring).
        ctx = _build_caller_info_blurb(caller_info)
        try:
            rag = _retrieve_and_generate(region, utterance, ctx, lang_code)
            answer = _voice_friendly(rag.text)
            retrieval_top_score = rag.top_score
            grounding_blocked = not rag.grounded
        except Exception as exc:  # noqa: BLE001
            answer = strings["bedrock_error_template"].format(exc=exc)
            grounding_blocked = True

    session_attributes = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    intent_payload: Dict[str, Any] = {"name": intent_name, "state": "Fulfilled"}
    if "slots" in intent and intent.get("slots") is not None:
        intent_payload["slots"] = intent.get("slots") or {}

    if close_conversation:
        spoken = answer
        response: Dict[str, Any] = {
            "sessionState": {
                "sessionAttributes": session_attributes,
                "dialogAction": {"type": "Close"},
                "intent": intent_payload,
            },
            "messages": [{"contentType": "PlainText", "content": spoken[:5000]}],
        }
    else:
        # Use dialogAction=Close (not ElicitIntent) so that the matched intent
        # survives back to Amazon Connect. Lex V2 strips sessionState.intent
        # whenever dialogAction is ElicitIntent (the session is considered
        # "open for any new intent"), which made Connect's flow conditions
        # fail to match PrepQuestionIntent and silently bypass the
        # GI_Reset_Fallback block -- so fallbackCount never reset to 0 and
        # any non-consecutive off-topic question still hung up the caller.
        # Close ends the per-turn Lex session, but Connect re-enters the
        # ConnectParticipantWithLexBot block on the next loop with the same
        # Contact ID, so the user-visible call/recording/transcript and our
        # GIConversationTurns sessionId all remain unchanged.
        spoken = f"{answer} {strings['follow_up_prompt']}"
        response = {
            "sessionState": {
                "sessionAttributes": session_attributes,
                "dialogAction": {"type": "Close"},
                "intent": intent_payload,
            },
            "messages": [{"contentType": "PlainText", "content": spoken[:5000]}],
        }

    # Preserve active contexts if present
    ss = event.get("sessionState") or {}
    if "activeContexts" in ss:
        response["sessionState"]["activeContexts"] = ss["activeContexts"]

    # Verification log -- runs for every turn, including goodbye/escalation,
    # so the GIConversationTurns table is a complete audit trail. We log AFTER
    # building the response, and any failure is swallowed so the call is not
    # disrupted by a logging error.
    _log_conversation_turn(
        session_id=session_id,
        user_text=utterance,
        bot_text=spoken,
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
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    print(json.dumps(lambda_handler(sample, None), indent=2))

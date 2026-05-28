"""Event-shape helpers.

Every function here takes a raw Lex event ``dict`` and returns a small,
typed piece of derived info. Splitting them out (instead of inlining in
the nodes) makes each one independently testable against synthetic
event payloads with no graph or AWS in play.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Set

from skills import Skill


def extract_lang_code(event: Dict[str, Any]) -> str:
    """Mirror the prod ``_extract_lang_code``: caller language comes from
    the ``langCode`` Lex session attribute set by the Connect flow's DTMF
    gate. Accepts both short ('es') and locale-style ('es_US') values;
    falls back to English for anything unrecognised so a misconfigured
    flow can never leave the caller without a usable bot."""

    attrs = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    raw = (attrs.get("langCode") or attrs.get("LangCode") or "").strip().lower()
    if raw.startswith("es"):
        return "es"
    return "en"


def extract_user_utterance(event: Dict[str, Any]) -> str:
    """Return the caller's transcribed utterance for this turn (with
    legacy ``messages[0].content.content`` fallback for direct invocation)."""

    trans = (event.get("inputTranscript") or "").strip()
    if trans:
        return trans
    msg = (event.get("messages") or [{}])[0]
    content = (msg.get("content") or {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    return ""


def extract_patient_id(event: Dict[str, Any]) -> Optional[str]:
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


def extract_caller_phone(event: Dict[str, Any]) -> Optional[str]:
    """Connect forwards ``$.CustomerEndpoint.Address`` as a session
    attribute named ``callerPhone``. We accept several aliases so this
    keeps working under direct Lambda invocation / flow refactors."""

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


def extract_contact_id(event: Dict[str, Any]) -> Optional[str]:
    """When Lex is invoked from Amazon Connect, the Lex ``sessionId`` is
    the Connect Contact ID. Also accept explicit attributes for direct
    invocation."""

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


def all_skip_placeholders(skills: Iterable[Skill]) -> Set[str]:
    """Return the union of every skill's ``skip_placeholder`` plus the
    prod-historical extras (``"no proporcionada"``) so the Bedrock prompt
    never sees a "not provided" string masquerading as a real slot value.

    Centralising this avoids the prod pattern where ``_SKIP_PLACEHOLDERS``
    was a hard-coded module-level set that had to be kept in sync with
    the per-language strings by hand. With skills, adding a language
    automatically extends this set."""

    placeholders = {skill.skip_placeholder.strip().lower() for skill in skills}
    # The prod module also accepts the feminine variant Spanish flows
    # historically used. Keep it here for backwards-compat with any old
    # Connect flow state that hasn't been redeployed.
    placeholders.add("no proporcionada")
    return placeholders


def extract_caller_info(
    event: Dict[str, Any],
    skip_placeholders: Set[str],
) -> Dict[str, str]:
    """Return per-call patient context the Connect flow's collection step
    saved into Lex session attributes. Skips slots whose value is one of
    the language-keyed "not provided" placeholders so a skipped slot
    never leaks into the Bedrock prompt as if it were the real value.

    Prefers ``procedureDate`` / ``procedureTime`` (normalised by Lex)
    over the raw transcript fallbacks ``procedureDate_display`` /
    ``procedureTime_display`` so the model gets unambiguous values; the
    raw transcript is used only when the normalised slot is empty or
    holds the skip placeholder."""

    attrs = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    info: Dict[str, str] = {}

    name_val = (attrs.get("patientName") or "").strip()
    if name_val and name_val.lower() not in skip_placeholders:
        info["patientName"] = name_val

    for slot_key, display_key in (
        ("procedureDate", "procedureDate_display"),
        ("procedureTime", "procedureTime_display"),
    ):
        normalised = (attrs.get(slot_key) or "").strip()
        if normalised and normalised.lower() not in skip_placeholders:
            info[slot_key] = normalised
            continue
        raw = (attrs.get(display_key) or "").strip()
        if raw and raw.lower() not in skip_placeholders:
            info[slot_key] = raw

    return info


def session_attributes(event: Dict[str, Any]) -> Dict[str, str]:
    """Shallow-copied session attributes from the event. Always returns a
    new dict so nodes can mutate freely without aliasing the event."""

    raw = (event.get("sessionState") or {}).get("sessionAttributes") or {}
    return dict(raw)


def build_caller_info_blurb(caller_info: Dict[str, str]) -> str:
    """Format per-call patient context for the Bedrock generation prompt.

    Kept in English regardless of caller language so the model sees a
    consistent structural cue; the skill's prompt template handles the
    response-language switch. Appended ONLY to the Stage-2 generation
    query, never the Stage-1 retrieval query (would skew similarity)."""

    if not caller_info:
        return ""
    lines = [
        "Caller-supplied context (apply only if the patient question is "
        "about timing or personalised scheduling):"
    ]
    if "patientName" in caller_info:
        lines.append(f"- Patient name: {caller_info['patientName']}")
    if "procedureDate" in caller_info:
        lines.append(f"- Procedure date: {caller_info['procedureDate']}")
    if "procedureTime" in caller_info:
        lines.append(f"- Procedure time: {caller_info['procedureTime']}")
    return "\n".join(lines)

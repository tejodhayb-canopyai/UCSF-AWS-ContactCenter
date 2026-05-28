"""Text shaping utilities.

These are intentionally tiny and pure (no AWS, no boto3, no state) so
they're cheap to import and easy to unit-test exhaustively.
"""

from __future__ import annotations

import re

from skills import Skill

from . import settings


def needs_escalation(text: str, skill: Skill) -> bool:
    t = text.lower()
    return any(k in t for k in skill.escalation_keywords)


def wants_to_end(text: str, skill: Skill) -> bool:
    t = text.lower().strip()
    return any(k in t for k in skill.end_conversation_keywords)


def normalize_name(transcript: str, skill: Skill) -> str:
    """Trim conversational prefixes from a name utterance so the
    confirmation playback and the Bedrock prompt see just the name.

    Keeps the original transcript if stripping the prefix would yield an
    empty string -- something is better than nothing for confirmation."""

    pattern = skill.compiled_name_prefix()
    stripped = pattern.sub("", transcript, count=1).strip()
    return stripped or transcript


def voice_friendly(text: str) -> str:
    """Strip filler phrases, collapse whitespace, and clamp to
    ``VOICE_MAX_CHARS`` on a sentence boundary."""

    cleaned = " ".join(text.split())
    cleaned = re.sub(r"^(answer|response):\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(
        r"^based on (the )?(search results|provided information),?\s*",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"^according to (the )?(search results|provided documents),?\s*",
        "",
        cleaned,
        flags=re.I,
    )

    if len(cleaned) <= settings.VOICE_MAX_CHARS:
        return cleaned

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    selected: list[str] = []
    total = 0
    for sentence in sentences:
        if not sentence:
            continue
        next_total = total + len(sentence) + (1 if selected else 0)
        if selected and next_total > settings.VOICE_MAX_CHARS:
            break
        selected.append(sentence)
        total = next_total
        if len(selected) >= 3:
            break

    shortened = " ".join(selected).strip()
    if shortened:
        return shortened
    return cleaned[: settings.VOICE_MAX_CHARS - 1].rstrip(" ,;:") + "."


def redact_name(name: str | None) -> str:
    """Mask a caller name for CloudWatch output. Keep first character so
    recurring patterns ("J***") are still spotable without leaking PHI."""

    if not name:
        return "-"
    return f"{name[0]}***"

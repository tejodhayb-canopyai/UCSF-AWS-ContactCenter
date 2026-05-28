"""Pydantic models that validate skill files at cold start.

Validation runs once when the container loads. Any malformed skill (missing
key, bad regex, missing Bedrock placeholder, etc.) raises a
``pydantic.ValidationError`` which crashes container init -- this is the
desired behavior: we want bad config to fail loud at deploy time, never on
a live call.
"""

from __future__ import annotations

import re
from typing import List

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Persona(BaseModel):
    """Free-form persona metadata. Currently informational only -- the
    Lambda does not consume these fields at runtime, but they live in the
    skill so a clinician reading the file knows who 'Lucy' is supposed to
    be and what role she plays for this language."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    role: str = Field(min_length=1)


class CannedStrings(BaseModel):
    """Bot-spoken strings that never go through Bedrock.

    These are the verbatim utterances the Lambda emits when it short-circuits
    Bedrock (escalation, goodbye, empty input, low-grounding fallback, bedrock
    error). Each language's strings are spoken by that language's Polly voice,
    so they must be written natively -- never machine-translated at runtime.

    ``bedrock_error_template`` is a Python ``str.format()`` template that must
    contain a ``{exc}`` placeholder so the Lambda can interpolate the actual
    exception message at runtime.
    """

    model_config = ConfigDict(extra="forbid")

    follow_up_prompt: str = Field(min_length=1)
    no_answer_fallback: str = Field(min_length=1)
    empty_input_fallback: str = Field(min_length=1)
    goodbye_message: str = Field(min_length=1)
    escalation_message: str = Field(min_length=1)
    bedrock_error_template: str = Field(min_length=1)

    @field_validator("bedrock_error_template")
    @classmethod
    def _has_exc_placeholder(cls, v: str) -> str:
        if "{exc}" not in v:
            raise ValueError(
                "bedrock_error_template must contain the '{exc}' placeholder "
                "so the Lambda can interpolate the underlying error message"
            )
        return v


class Skill(BaseModel):
    """A language pack. One per language; loaded from ``skills/<lang>.md``.

    The markdown body of the file (everything after the YAML frontmatter)
    becomes ``prompt_template`` and is sent verbatim to Bedrock as the
    ``textPromptTemplate`` for ``retrieve_and_generate``. The ``$search_results$``
    and ``$query$`` tokens MUST be preserved because Bedrock substitutes them
    server-side; if they were missing, Bedrock would silently produce
    ungrounded answers.
    """

    model_config = ConfigDict(extra="forbid")

    language: str = Field(
        min_length=2,
        max_length=2,
        description="Short ISO 639-1 code, e.g. 'en' or 'es'. Lowercase.",
    )
    locale_codes: List[str] = Field(
        min_length=1,
        description=(
            "All locale strings the Connect flow may set in the langCode "
            "session attribute that should resolve to this skill. E.g. "
            "['en', 'en_US', 'en-US']. The first entry is canonical."
        ),
    )
    persona: Persona
    polly_voice: str = Field(
        min_length=1,
        description=(
            "Polly Neural voice id Connect uses for this language (for "
            "documentation only -- voice selection happens in the Connect "
            "flow, not in the Lambda)."
        ),
    )
    escalation_keywords: List[str] = Field(min_length=1)
    end_conversation_keywords: List[str] = Field(min_length=1)
    name_prefix_pattern: str = Field(
        min_length=1,
        description=(
            "Regex (Python flavor) matched case-insensitively against the "
            "start of a CollectNameIntent utterance to strip conversational "
            "prefixes (e.g. 'my name is ', 'me llamo ')."
        ),
    )
    skip_placeholder: str = Field(
        min_length=1,
        description=(
            "Native-language string the Lambda writes into a slot's "
            "*_display attribute when the caller skipped (timed out or "
            "pressed #). Must match the value the Connect flow seeds via "
            "GI_Set_Attrs_<LANG> so the confirmation playback reads "
            "consistently."
        ),
    )
    canned: CannedStrings
    prompt_template: str = Field(
        min_length=1,
        description=(
            "The markdown body of the skill file, passed verbatim to Bedrock "
            "as the retrieve_and_generate textPromptTemplate. Must contain "
            "the literal tokens $search_results$ and $query$."
        ),
    )

    @field_validator("language")
    @classmethod
    def _language_lowercase(cls, v: str) -> str:
        if v != v.lower():
            raise ValueError("language code must be lowercase (e.g. 'en', not 'EN')")
        return v

    @field_validator("locale_codes")
    @classmethod
    def _locale_codes_nonempty(cls, v: List[str]) -> List[str]:
        cleaned = [code.strip() for code in v if code and code.strip()]
        if not cleaned:
            raise ValueError("locale_codes must contain at least one non-empty entry")
        return cleaned

    @field_validator("name_prefix_pattern")
    @classmethod
    def _pattern_compiles(cls, v: str) -> str:
        try:
            re.compile(v, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"name_prefix_pattern is not a valid regex: {exc}") from exc
        return v

    @field_validator("prompt_template")
    @classmethod
    def _has_bedrock_placeholders(cls, v: str) -> str:
        missing = [tok for tok in ("$search_results$", "$query$") if tok not in v]
        if missing:
            raise ValueError(
                "prompt_template is missing required Bedrock placeholder(s): "
                f"{', '.join(missing)}. Bedrock substitutes these server-side; "
                "without them the model receives an empty prompt."
            )
        return v

    @model_validator(mode="after")
    def _language_in_locale_codes(self) -> "Skill":
        if self.language not in self.locale_codes:
            raise ValueError(
                f"language '{self.language}' must also appear in locale_codes "
                f"(got {self.locale_codes})"
            )
        return self

    def compiled_name_prefix(self) -> "re.Pattern[str]":
        """Cached-friendly accessor used by the Lambda's name normaliser."""
        return re.compile(self.name_prefix_pattern, re.IGNORECASE)

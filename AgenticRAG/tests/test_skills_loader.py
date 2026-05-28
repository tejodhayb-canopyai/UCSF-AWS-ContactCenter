"""Unit tests for the skill loader and schema.

Run from the AgenticRAG folder:

    python -m pytest tests/ -v

These tests are designed to run offline -- no AWS, no Bedrock, no Lex.
They lock in the contract that ``skills/<lang>.md`` files faithfully
mirror the production ``LANG_STRINGS`` and ``PROMPT_TEMPLATES`` dicts
from ``../lambda_handler.py``, so the Phase 2 LangGraph wrapper can
swap in this loader as a drop-in source of truth.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from skills import CannedStrings, Persona, Skill, load_skill, load_skills, resolve_skill
from skills.loader import DEFAULT_SKILLS_DIR

SKILLS_DIR = DEFAULT_SKILLS_DIR


# ---------------------------------------------------------------------------
# Happy-path loading of the shipped en.md / es.md
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def all_skills():
    return load_skills(SKILLS_DIR)


def test_loads_both_shipped_languages(all_skills):
    assert set(all_skills.keys()) == {"en", "es"}


def test_each_skill_is_a_validated_Skill_instance(all_skills):
    for code, skill in all_skills.items():
        assert isinstance(skill, Skill)
        assert skill.language == code


# ---------------------------------------------------------------------------
# English skill — exact-match assertions against prod LANG_STRINGS["en"]
# ---------------------------------------------------------------------------


def test_english_persona_and_voice(all_skills):
    en = all_skills["en"]
    assert en.persona.name == "Lucy"
    assert en.polly_voice == "Danielle"
    assert "en_US" in en.locale_codes


def test_english_escalation_keywords_match_prod(all_skills):
    en = all_skills["en"]
    expected = {
        "chest pain",
        "can't breathe",
        "cannot breathe",
        "bleeding heavily",
        "passed out",
        "fainted",
        "severe pain",
        "suicide",
        "kill myself",
    }
    assert set(en.escalation_keywords) == expected


def test_english_end_conversation_keywords_match_prod(all_skills):
    en = all_skills["en"]
    expected = {
        "bye",
        "goodbye",
        "that's all",
        "that is all",
        "no thanks",
        "no thank you",
        "i'm done",
        "im done",
    }
    assert set(en.end_conversation_keywords) == expected


def test_english_canned_strings_match_prod(all_skills):
    en = all_skills["en"]
    assert en.canned.follow_up_prompt == "What else can I help with?"
    assert en.canned.goodbye_message == "Thank you for calling. Goodbye."
    assert en.canned.empty_input_fallback == "I didn't catch that. Please ask your GI prep question."
    assert "approved prep documents" in en.canned.no_answer_fallback
    assert "emergency symptoms" in en.canned.escalation_message
    assert "{exc}" in en.canned.bedrock_error_template


def test_english_skip_placeholder(all_skills):
    assert all_skills["en"].skip_placeholder == "not provided"


def test_english_name_prefix_pattern_strips_prefixes(all_skills):
    pattern = all_skills["en"].compiled_name_prefix()
    cases = {
        "my name is Tejodhay": "Tejodhay",
        "MY NAME IS Priya": "Priya",
        "the name is Ananya": "Ananya",
        "I am Rajan": "Rajan",
        "I'm Sara": "Sara",
        "Im Sara": "Sara",
        "this is Mike": "Mike",
        "call me Lucy": "Lucy",
    }
    for utterance, expected_remainder in cases.items():
        stripped = pattern.sub("", utterance, count=1).strip()
        assert stripped == expected_remainder, f"failed for: {utterance!r}"


def test_english_prompt_template_preserves_bedrock_placeholders(all_skills):
    body = all_skills["en"].prompt_template
    assert "$search_results$" in body
    assert "$query$" in body
    assert "NO_ANSWER_FOUND" in body
    assert "Lucy" in body
    assert body.lstrip().startswith("You are Lucy")


# ---------------------------------------------------------------------------
# Spanish skill — exact-match assertions against prod LANG_STRINGS["es"]
# ---------------------------------------------------------------------------


def test_spanish_persona_and_voice(all_skills):
    es = all_skills["es"]
    assert es.persona.name == "Lucy"
    assert es.polly_voice == "Lupe"
    assert "es_US" in es.locale_codes


def test_spanish_escalation_keywords_include_accented(all_skills):
    es = all_skills["es"]
    assert "dolor de pecho" in es.escalation_keywords
    assert "me desmayé" in es.escalation_keywords
    assert "perdí el conocimiento" in es.escalation_keywords
    assert "suicidio" in es.escalation_keywords


def test_spanish_end_conversation_keywords_include_accented(all_skills):
    es = all_skills["es"]
    assert "adiós" in es.end_conversation_keywords
    assert "ya terminé" in es.end_conversation_keywords


def test_spanish_canned_strings_translated(all_skills):
    es = all_skills["es"]
    assert es.canned.goodbye_message == "Gracias por llamar. Adiós."
    assert "preparación" in es.canned.no_answer_fallback
    assert "{exc}" in es.canned.bedrock_error_template


def test_spanish_skip_placeholder(all_skills):
    assert all_skills["es"].skip_placeholder == "no proporcionado"


def test_spanish_name_prefix_pattern_strips_prefixes(all_skills):
    pattern = all_skills["es"].compiled_name_prefix()
    cases = {
        "me llamo Tejodhay": "Tejodhay",
        "Me Llamo Priya": "Priya",
        "mi nombre es Ananya": "Ananya",
        "yo soy Rajan": "Rajan",
        "soy Sara": "Sara",
        "llamame Mike": "Mike",
        "llámame Lucy": "Lucy",
    }
    for utterance, expected_remainder in cases.items():
        stripped = pattern.sub("", utterance, count=1).strip()
        assert stripped == expected_remainder, f"failed for: {utterance!r}"


def test_spanish_prompt_template_preserves_bedrock_placeholders(all_skills):
    body = all_skills["es"].prompt_template
    assert "$search_results$" in body
    assert "$query$" in body
    assert "NO_ANSWER_FOUND" in body
    assert body.lstrip().startswith("Eres Lucy")


# ---------------------------------------------------------------------------
# resolve_skill — Lex/Connect-style locale-code lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lang_code,expected",
    [
        ("en", "en"),
        ("en_US", "en"),
        ("EN_US", "en"),  # case-insensitive
        ("en-US", "en"),  # dash style
        ("es", "es"),
        ("es_US", "es"),
        ("es-MX", "es"),
        ("ES", "es"),
    ],
)
def test_resolve_skill_known_locales(all_skills, lang_code, expected):
    assert resolve_skill(all_skills, lang_code).language == expected


@pytest.mark.parametrize("lang_code", ["", "xx", "fr", "xyz", "zh_CN"])
def test_resolve_skill_unknown_falls_back_to_english(all_skills, lang_code):
    assert resolve_skill(all_skills, lang_code).language == "en"


def test_resolve_skill_missing_default_raises(all_skills):
    with pytest.raises(KeyError):
        resolve_skill(all_skills, "fr", default="ja")


# ---------------------------------------------------------------------------
# Schema validation — rejects malformed skill files
# ---------------------------------------------------------------------------


def _valid_payload() -> dict:
    """Helper: minimum valid frontmatter + prompt_template."""
    return {
        "language": "en",
        "locale_codes": ["en", "en_US"],
        "persona": {"name": "Lucy", "role": "tester"},
        "polly_voice": "Danielle",
        "escalation_keywords": ["chest pain"],
        "end_conversation_keywords": ["bye"],
        "name_prefix_pattern": r"^(?:my name is)\s+",
        "skip_placeholder": "not provided",
        "canned": {
            "follow_up_prompt": "anything else?",
            "no_answer_fallback": "no idea",
            "empty_input_fallback": "say again?",
            "goodbye_message": "bye",
            "escalation_message": "calling staff",
            "bedrock_error_template": "oops ({exc})",
        },
        "prompt_template": "stub $search_results$ $query$",
    }


def test_schema_rejects_missing_field():
    p = _valid_payload()
    del p["polly_voice"]
    with pytest.raises(ValidationError):
        Skill.model_validate(p)


def test_schema_rejects_uppercase_language():
    p = _valid_payload()
    p["language"] = "EN"
    with pytest.raises(ValidationError):
        Skill.model_validate(p)


def test_schema_rejects_language_not_in_locale_codes():
    p = _valid_payload()
    p["language"] = "es"  # but locale_codes is still ["en", "en_US"]
    with pytest.raises(ValidationError):
        Skill.model_validate(p)


def test_schema_rejects_prompt_template_without_search_results():
    p = _valid_payload()
    p["prompt_template"] = "no placeholders here just $query$"
    with pytest.raises(ValidationError) as exc_info:
        Skill.model_validate(p)
    assert "$search_results$" in str(exc_info.value)


def test_schema_rejects_prompt_template_without_query():
    p = _valid_payload()
    p["prompt_template"] = "missing the other one $search_results$"
    with pytest.raises(ValidationError) as exc_info:
        Skill.model_validate(p)
    assert "$query$" in str(exc_info.value)


def test_schema_rejects_invalid_regex():
    p = _valid_payload()
    p["name_prefix_pattern"] = "(unclosed"
    with pytest.raises(ValidationError):
        Skill.model_validate(p)


def test_schema_rejects_bedrock_error_template_without_exc_placeholder():
    p = _valid_payload()
    p["canned"]["bedrock_error_template"] = "error occurred"
    with pytest.raises(ValidationError) as exc_info:
        Skill.model_validate(p)
    assert "{exc}" in str(exc_info.value)


def test_schema_rejects_extra_unknown_fields():
    p = _valid_payload()
    p["mystery_field"] = "shouldnt be here"
    with pytest.raises(ValidationError):
        Skill.model_validate(p)


# ---------------------------------------------------------------------------
# Loader I/O edge cases
# ---------------------------------------------------------------------------


def test_load_skill_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_skill(tmp_path / "does_not_exist.md")


def test_load_skill_no_frontmatter_raises(tmp_path):
    f = tmp_path / "bad.md"
    f.write_text("just a markdown body, no frontmatter", encoding="utf-8")
    with pytest.raises(ValueError, match="no YAML frontmatter"):
        load_skill(f)


def test_load_skill_empty_body_raises(tmp_path):
    f = tmp_path / "empty_body.md"
    f.write_text(
        "---\nlanguage: en\nlocale_codes: [en, en_US]\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="empty markdown body"):
        load_skill(f)


def test_load_skills_empty_dir_raises(tmp_path):
    with pytest.raises(ValueError, match="No skill files"):
        load_skills(tmp_path)


def test_load_skills_duplicate_language_raises(tmp_path):
    payload_en = (SKILLS_DIR / "en.md").read_text(encoding="utf-8")
    (tmp_path / "en.md").write_text(payload_en, encoding="utf-8")
    (tmp_path / "english.md").write_text(payload_en, encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate skill"):
        load_skills(tmp_path)

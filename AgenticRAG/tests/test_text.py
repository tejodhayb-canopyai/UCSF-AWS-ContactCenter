"""Unit tests for agentic.text helpers."""

from __future__ import annotations

import pytest

from agentic import text
from skills import load_skills
from skills.loader import DEFAULT_SKILLS_DIR


@pytest.fixture(scope="module")
def en_skill():
    return load_skills(DEFAULT_SKILLS_DIR)["en"]


@pytest.fixture(scope="module")
def es_skill():
    return load_skills(DEFAULT_SKILLS_DIR)["es"]


# ---------------------------------------------------------------------------
# needs_escalation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    [
        "I have chest pain",
        "I CAN'T BREATHE",
        "my dad has severe pain",
        "I am bleeding heavily right now",
    ],
)
def test_needs_escalation_en_triggers(en_skill, utterance):
    assert text.needs_escalation(utterance, en_skill)


@pytest.mark.parametrize(
    "utterance",
    [
        "Tengo dolor de pecho",
        "no puedo respirar",
        "Me desmaye en la mañana",
        "tengo dolor severo",
    ],
)
def test_needs_escalation_es_triggers(es_skill, utterance):
    assert text.needs_escalation(utterance, es_skill)


def test_needs_escalation_normal_question_does_not_trigger(en_skill):
    assert not text.needs_escalation("when should I start my prep?", en_skill)


# ---------------------------------------------------------------------------
# wants_to_end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance", ["bye", "goodbye", "  that's all  ", "no thanks", "I'm done"]
)
def test_wants_to_end_en_triggers(en_skill, utterance):
    assert text.wants_to_end(utterance, en_skill)


@pytest.mark.parametrize("utterance", ["adios", "adiós", "ya terminé", "no gracias"])
def test_wants_to_end_es_triggers(es_skill, utterance):
    assert text.wants_to_end(utterance, es_skill)


def test_wants_to_end_normal_question_does_not_trigger(en_skill):
    assert not text.wants_to_end("when should I drink the prep?", en_skill)


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Tejodhay", "Tejodhay"),
        ("my name is Tejodhay", "Tejodhay"),
        ("MY NAME IS Priya", "Priya"),
        ("the name is Ananya", "Ananya"),
        ("I am Rajan", "Rajan"),
        ("I'm Sara", "Sara"),
        ("Im Sara", "Sara"),
        ("this is Mike", "Mike"),
        ("call me Lucy", "Lucy"),
    ],
)
def test_normalize_name_en(en_skill, raw, expected):
    assert text.normalize_name(raw, en_skill) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Tejodhay", "Tejodhay"),
        ("me llamo Tejodhay", "Tejodhay"),
        ("Me Llamo Priya", "Priya"),
        ("mi nombre es Ananya", "Ananya"),
        ("yo soy Rajan", "Rajan"),
        ("soy Sara", "Sara"),
        ("llamame Mike", "Mike"),
        ("llámame Lucy", "Lucy"),
    ],
)
def test_normalize_name_es(es_skill, raw, expected):
    assert text.normalize_name(raw, es_skill) == expected


def test_normalize_name_empty_after_strip_keeps_original(en_skill):
    # Edge case: "my name is " by itself strips to empty; we return the
    # original transcript verbatim (trailing space included) so Polly
    # has *something* to play back. Matches prod _normalize_name exactly.
    assert text.normalize_name("my name is ", en_skill) == "my name is "


# ---------------------------------------------------------------------------
# voice_friendly
# ---------------------------------------------------------------------------


def test_voice_friendly_collapses_whitespace():
    assert text.voice_friendly("  one  two\n\nthree  ") == "one two three"


def test_voice_friendly_strips_answer_prefix():
    assert text.voice_friendly("Answer: drink the prep slowly.") == "drink the prep slowly."


def test_voice_friendly_strips_based_on_prefix():
    assert text.voice_friendly(
        "based on the search results, start your prep at 6 PM."
    ) == "start your prep at 6 PM."


def test_voice_friendly_short_text_unchanged(en_skill):
    text_in = "Drink the prep at 6 PM and stop eating solid food at 4 PM."
    assert text.voice_friendly(text_in) == text_in


def test_voice_friendly_truncates_long_text_on_sentence_boundary():
    long = (
        "First sentence here. " * 10
        + "Second sentence here. " * 10
        + "Third sentence here. " * 10
    )
    out = text.voice_friendly(long)
    assert len(out) <= 651  # VOICE_MAX_CHARS + small tolerance for ending dot


# ---------------------------------------------------------------------------
# redact_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Tejodhay", "T***"),
        ("priya", "p***"),
        ("", "-"),
        (None, "-"),
    ],
)
def test_redact_name(name, expected):
    assert text.redact_name(name) == expected

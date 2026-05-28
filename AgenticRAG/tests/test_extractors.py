"""Unit tests for agentic.extractors against synthetic Lex events."""

from __future__ import annotations

import pytest

from agentic import extractors
from skills import load_skills
from skills.loader import DEFAULT_SKILLS_DIR


@pytest.fixture(scope="module")
def all_skills():
    return load_skills(DEFAULT_SKILLS_DIR)


@pytest.fixture
def skip_set(all_skills):
    return extractors.all_skip_placeholders(all_skills.values())


# ---------------------------------------------------------------------------
# extract_lang_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "session_attrs,expected",
    [
        ({}, "en"),
        ({"langCode": "en"}, "en"),
        ({"langCode": "en_US"}, "en"),
        ({"langCode": "es"}, "es"),
        ({"langCode": "es_US"}, "es"),
        ({"langCode": "ES_MX"}, "es"),
        ({"langCode": "ES-MX"}, "es"),
        ({"LangCode": "es"}, "es"),
        ({"langCode": "fr"}, "en"),
        ({"langCode": ""}, "en"),
    ],
)
def test_extract_lang_code(session_attrs, expected):
    event = {"sessionState": {"sessionAttributes": session_attrs}}
    assert extractors.extract_lang_code(event) == expected


# ---------------------------------------------------------------------------
# extract_user_utterance
# ---------------------------------------------------------------------------


def test_extract_user_utterance_from_inputTranscript():
    event = {"inputTranscript": "  hello world  "}
    assert extractors.extract_user_utterance(event) == "hello world"


def test_extract_user_utterance_empty_returns_empty_string():
    assert extractors.extract_user_utterance({}) == ""


def test_extract_user_utterance_fallback_to_messages():
    event = {"messages": [{"content": {"content": "  fallback text  "}}]}
    assert extractors.extract_user_utterance(event) == "fallback text"


# ---------------------------------------------------------------------------
# extract_patient_id
# ---------------------------------------------------------------------------


def test_extract_patient_id_from_attrs():
    event = {"sessionState": {"sessionAttributes": {"patientId": "P-123"}}}
    assert extractors.extract_patient_id(event) == "P-123"


def test_extract_patient_id_from_slot():
    event = {
        "sessionState": {
            "intent": {
                "slots": {"patientId": {"value": {"interpretedValue": "P-456"}}}
            }
        }
    }
    assert extractors.extract_patient_id(event) == "P-456"


def test_extract_patient_id_missing_returns_none():
    assert extractors.extract_patient_id({}) is None


# ---------------------------------------------------------------------------
# extract_caller_phone
# ---------------------------------------------------------------------------


def test_extract_caller_phone_from_session_attr():
    event = {"sessionState": {"sessionAttributes": {"callerPhone": "+14155551212"}}}
    assert extractors.extract_caller_phone(event) == "+14155551212"


def test_extract_caller_phone_from_request_attr():
    event = {"requestAttributes": {"x-amz-lex:channels:platform:caller": "+14155551212"}}
    assert extractors.extract_caller_phone(event) == "+14155551212"


def test_extract_caller_phone_missing_returns_none():
    assert extractors.extract_caller_phone({}) is None


# ---------------------------------------------------------------------------
# extract_contact_id
# ---------------------------------------------------------------------------


def test_extract_contact_id_falls_back_to_sessionId():
    event = {"sessionId": "connect-contact-uuid"}
    assert extractors.extract_contact_id(event) == "connect-contact-uuid"


def test_extract_contact_id_attribute_wins_over_sessionId():
    event = {
        "sessionId": "other-uuid",
        "sessionState": {"sessionAttributes": {"contactId": "C-explicit"}},
    }
    assert extractors.extract_contact_id(event) == "C-explicit"


# ---------------------------------------------------------------------------
# all_skip_placeholders
# ---------------------------------------------------------------------------


def test_all_skip_placeholders_includes_shipped_languages(skip_set):
    assert "not provided" in skip_set
    assert "no proporcionado" in skip_set
    # Backwards-compat with the feminine Spanish historically used.
    assert "no proporcionada" in skip_set


# ---------------------------------------------------------------------------
# extract_caller_info
# ---------------------------------------------------------------------------


def test_extract_caller_info_picks_up_all_three(skip_set):
    event = {
        "sessionState": {
            "sessionAttributes": {
                "patientName": "Tejodhay",
                "procedureDate": "2026-05-22",
                "procedureTime": "09:00",
            }
        }
    }
    info = extractors.extract_caller_info(event, skip_set)
    assert info == {
        "patientName": "Tejodhay",
        "procedureDate": "2026-05-22",
        "procedureTime": "09:00",
    }


def test_extract_caller_info_skips_placeholder(skip_set):
    event = {
        "sessionState": {
            "sessionAttributes": {
                "patientName": "not provided",
                "procedureDate": "2026-05-22",
                "procedureTime": "no proporcionado",
            }
        }
    }
    info = extractors.extract_caller_info(event, skip_set)
    assert "patientName" not in info
    assert info["procedureDate"] == "2026-05-22"
    assert "procedureTime" not in info


def test_extract_caller_info_falls_back_to_display(skip_set):
    event = {
        "sessionState": {
            "sessionAttributes": {
                "procedureDate": "",
                "procedureDate_display": "May 22",
                "procedureTime": "not provided",
                "procedureTime_display": "9 AM",
            }
        }
    }
    info = extractors.extract_caller_info(event, skip_set)
    assert info["procedureDate"] == "May 22"
    assert info["procedureTime"] == "9 AM"


def test_extract_caller_info_empty_event_returns_empty_dict(skip_set):
    assert extractors.extract_caller_info({}, skip_set) == {}


# ---------------------------------------------------------------------------
# build_caller_info_blurb
# ---------------------------------------------------------------------------


def test_build_caller_info_blurb_empty_returns_empty():
    assert extractors.build_caller_info_blurb({}) == ""


def test_build_caller_info_blurb_includes_only_present_fields():
    blurb = extractors.build_caller_info_blurb(
        {"patientName": "Tejodhay", "procedureDate": "May 22"}
    )
    assert "Caller-supplied context" in blurb
    assert "Patient name: Tejodhay" in blurb
    assert "Procedure date: May 22" in blurb
    assert "Procedure time" not in blurb

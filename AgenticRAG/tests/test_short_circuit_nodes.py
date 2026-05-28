"""Unit tests for the short-circuit nodes (name_dialog, collection,
fallback_close). These nodes write a complete Lex response into state
and bypass the Q&A tail."""

from __future__ import annotations

import pytest

from agentic.nodes import collection as collection_mod
from agentic.nodes import fallback_close as fb_mod
from agentic.nodes import name_dialog as nd_mod
from agentic.nodes.setup import setup_node
from skills import load_skills
from skills.loader import DEFAULT_SKILLS_DIR


@pytest.fixture(scope="module")
def all_skills():
    return load_skills(DEFAULT_SKILLS_DIR)


def _run_setup(event):
    return {"event": event, **setup_node({"event": event})}


# ---------------------------------------------------------------------------
# name_dialog_node
# ---------------------------------------------------------------------------


def test_name_dialog_slot_filled_advances_to_fulfillment():
    event = {
        "invocationSource": "DialogCodeHook",
        "inputTranscript": "Tejodhay",
        "sessionState": {
            "sessionAttributes": {"langCode": "en"},
            "intent": {
                "name": "CollectNameIntent",
                "slots": {
                    "patientName": {"value": {"interpretedValue": "Tejodhay"}}
                },
            },
        },
    }
    state = _run_setup(event)
    out = nd_mod.name_dialog_node(state)
    resp = out["response"]
    assert resp["sessionState"]["dialogAction"]["type"] == "FulfillIntent"
    assert resp["sessionState"]["intent"]["state"] == "ReadyForFulfillment"
    assert out["branch"].startswith("name_dialog/")


def test_name_dialog_empty_slot_first_attempt_english_offers_spell_by_letter():
    event = {
        "invocationSource": "DialogCodeHook",
        "inputTranscript": "",
        "sessionState": {
            "sessionAttributes": {"langCode": "en"},
            "intent": {
                "name": "CollectNameIntent",
                "slots": {"patientName": None},
            },
        },
    }
    state = _run_setup(event)
    out = nd_mod.name_dialog_node(state)
    resp = out["response"]
    da = resp["sessionState"]["dialogAction"]
    assert da["type"] == "ElicitSlot"
    assert da["slotToElicit"] == "patientName"
    assert da["slotElicitationStyle"] == "SpellByLetter"
    # Counter advanced for next attempt.
    assert resp["sessionState"]["sessionAttributes"]["nameDialogRetry"] == "1"


def test_name_dialog_second_attempt_gives_up():
    event = {
        "invocationSource": "DialogCodeHook",
        "inputTranscript": "",
        "sessionState": {
            "sessionAttributes": {"langCode": "en", "nameDialogRetry": "1"},
            "intent": {
                "name": "CollectNameIntent",
                "slots": {"patientName": None},
            },
        },
    }
    state = _run_setup(event)
    out = nd_mod.name_dialog_node(state)
    da = out["response"]["sessionState"]["dialogAction"]
    assert da["type"] == "FulfillIntent"


def test_name_dialog_spanish_skips_spell_by_letter():
    """Per AWS docs, slotElicitationStyle=SpellByLetter is English-only."""
    event = {
        "invocationSource": "DialogCodeHook",
        "inputTranscript": "",
        "sessionState": {
            "sessionAttributes": {"langCode": "es"},
            "intent": {
                "name": "CollectNameIntent",
                "slots": {"patientName": None},
            },
        },
    }
    state = _run_setup(event)
    out = nd_mod.name_dialog_node(state)
    da = out["response"]["sessionState"]["dialogAction"]
    assert da["type"] == "FulfillIntent"


# ---------------------------------------------------------------------------
# collection_node
# ---------------------------------------------------------------------------


def _make_collection_event(intent_name, transcript, lang="en", extra_attrs=None):
    attrs = {"langCode": lang}
    if extra_attrs:
        attrs.update(extra_attrs)
    return {
        "invocationSource": "FulfillmentCodeHook",
        "inputTranscript": transcript,
        "sessionState": {
            "sessionAttributes": attrs,
            "intent": {"name": intent_name, "slots": {}},
        },
    }


def test_collection_name_normalizes_prefix():
    event = _make_collection_event("CollectNameIntent", "my name is Tejodhay")
    state = _run_setup(event)
    out = collection_mod.collection_node(state)
    attrs = out["response"]["sessionState"]["sessionAttributes"]
    assert attrs["patientNameRaw"] == "Tejodhay"
    # collectionMode is cleared after capturing the name.
    assert attrs.get("collectionMode") == ""
    assert out["response"]["sessionState"]["intent"]["name"] == "CollectNameIntent"
    assert out["response"]["sessionState"]["intent"]["state"] == "Fulfilled"
    assert out["response"]["messages"] == []


def test_collection_date_writes_raw_transcript_unchanged():
    event = _make_collection_event("CollectDateIntent", "May twenty second")
    state = _run_setup(event)
    out = collection_mod.collection_node(state)
    attrs = out["response"]["sessionState"]["sessionAttributes"]
    assert attrs["procedureDateRaw"] == "May twenty second"


def test_collection_time_writes_raw_transcript_unchanged():
    event = _make_collection_event("CollectTimeIntent", "nine in the morning")
    state = _run_setup(event)
    out = collection_mod.collection_node(state)
    attrs = out["response"]["sessionState"]["sessionAttributes"]
    assert attrs["procedureTimeRaw"] == "nine in the morning"


def test_collection_empty_transcript_writes_skip_placeholder_en():
    event = _make_collection_event("CollectDateIntent", "")
    state = _run_setup(event)
    out = collection_mod.collection_node(state)
    attrs = out["response"]["sessionState"]["sessionAttributes"]
    assert attrs["procedureDateRaw"] == "not provided"


def test_collection_empty_transcript_writes_skip_placeholder_es():
    event = _make_collection_event("CollectDateIntent", "", lang="es")
    state = _run_setup(event)
    out = collection_mod.collection_node(state)
    attrs = out["response"]["sessionState"]["sessionAttributes"]
    assert attrs["procedureDateRaw"] == "no proporcionado"


def test_collection_dtmf_hash_is_treated_as_skip():
    event = _make_collection_event("CollectTimeIntent", "#")
    state = _run_setup(event)
    out = collection_mod.collection_node(state)
    attrs = out["response"]["sessionState"]["sessionAttributes"]
    assert attrs["procedureTimeRaw"] == "not provided"


def test_collection_fallback_guard_routes_to_name():
    """FallbackIntent + collectionMode=name should be re-routed by setup
    so collection_node writes ``patientNameRaw`` even though the upstream
    intent name was ``FallbackIntent``."""
    event = {
        "invocationSource": "FulfillmentCodeHook",
        "inputTranscript": "Tejodhay",
        "sessionState": {
            "sessionAttributes": {"langCode": "en", "collectionMode": "name"},
            "intent": {"name": "FallbackIntent", "slots": {}},
        },
    }
    state = _run_setup(event)
    assert state["effective_intent"] == "CollectNameIntent"
    assert state["is_fallback_name_guard"] is True
    out = collection_mod.collection_node(state)
    attrs = out["response"]["sessionState"]["sessionAttributes"]
    assert attrs["patientNameRaw"] == "Tejodhay"
    assert attrs.get("collectionMode") == ""
    assert out["branch"] == "collection/fallback_guard"


# ---------------------------------------------------------------------------
# fallback_close_node
# ---------------------------------------------------------------------------


def test_fallback_close_returns_empty_messages_and_closes():
    event = {
        "invocationSource": "FulfillmentCodeHook",
        "inputTranscript": "what time is the football game?",
        "sessionState": {
            "sessionAttributes": {"langCode": "en", "fallbackCount": "1"},
            "intent": {"name": "FallbackIntent", "slots": {}},
        },
    }
    state = _run_setup(event)
    # Sanity check: not fallback-guard, just plain Q&A fallback.
    assert state["is_fallback_name_guard"] is False
    out = fb_mod.fallback_close_node(state)
    resp = out["response"]
    assert resp["messages"] == []
    assert resp["sessionState"]["dialogAction"]["type"] == "Close"
    assert resp["sessionState"]["intent"]["name"] == "FallbackIntent"
    # sessionAttributes preserved unchanged.
    assert resp["sessionState"]["sessionAttributes"]["fallbackCount"] == "1"

"""Offline behavior tests for the read-only Duolingo plugin."""

from __future__ import annotations

import json
from unittest.mock import patch


def test_registers_only_the_duolingo_toolset():
    import plugins.duolingo as plugin

    calls = []

    class Context:
        def register_tool(self, **kwargs):
            calls.append(kwargs)

    plugin.register(Context())

    assert {call["name"] for call in calls} == {
        "duolingo_profile",
        "duolingo_review_queue",
        "duolingo_assess_conversation",
    }
    assert {call["toolset"] for call in calls} == {"duolingo"}


def test_client_sends_bearer_token_and_only_gets():
    from plugins.duolingo.client import DuolingoClient

    class Response:
        def read(self):
            return b'{"users": []}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    with patch("plugins.duolingo.client.urlopen", return_value=Response()) as request:
        assert DuolingoClient("token", base_url="https://example.test").get_user("learner") == {"users": []}

    sent = request.call_args.args[0]
    assert sent.get_method() == "GET"
    assert sent.get_header("Authorization") == "Bearer token"
    assert sent.full_url == "https://example.test/2017-06-30/users?username=learner"


def test_review_queue_prefers_low_numeric_strength(monkeypatch):
    from plugins.duolingo import tools

    monkeypatch.setattr(tools, "_client_or_error", lambda: type("Client", (), {
        "get_vocabulary_overview": lambda _self, _user_id: {
            "vocab_overview": [
                {"word_string": "alto", "strength": 0.9},
                {"word_string": "bajo", "strength": 0.2},
            ],
        },
    })())

    result = json.loads(tools.handle_review_queue({"user_id": 1, "limit": 1}))
    assert result["review_items"] == [{"word": "bajo", "strength": 0.2}]


def test_conversation_assessment_uses_learner_turns_only():
    from plugins.duolingo.tools import handle_assess_conversation

    result = json.loads(handle_assess_conversation({
        "transcript": "Tutor: Say casa and perro.\nStudent: Mi casa es grande.",
        "target_vocabulary": ["casa", "perro"],
    }))

    assert result["used"] == ["casa"]
    assert result["not_demonstrated"] == ["perro"]
    assert result["coverage"] == 0.5
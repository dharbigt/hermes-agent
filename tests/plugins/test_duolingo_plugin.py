"""Offline behavior tests for the read-only Duolingo plugin."""

from __future__ import annotations

import json
from unittest.mock import patch

from plugins.duolingo.client import (
    DuolingoClient,
    progressed_skills_from_course,
    select_language_course,
)


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


def test_client_posts_learned_lexemes_with_progressed_skills():
    class Response:
        def read(self):
            return b'{"learnedLexemes": [], "pagination": {"totalLexemes": 0}}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    skills = [{"finishedLevels": 1, "finishedSessions": 2, "skillId": {"id": "abc"}}]
    with patch("plugins.duolingo.client.urlopen", return_value=Response()) as request:
        payload = DuolingoClient("token", base_url="https://example.test").get_learned_lexemes(
            10883434, "fr", "en", skills, limit=10
        )

    assert payload["pagination"]["totalLexemes"] == 0
    sent = request.call_args.args[0]
    assert sent.get_method() == "POST"
    assert sent.full_url == (
        "https://example.test/2017-06-30/users/10883434/courses/fr/en/learned-lexemes"
        "?limit=10&sortBy=LEARNED_DATE&startIndex=0"
    )
    assert json.loads(sent.data.decode("utf-8")) == {
        "lastTotalLexemeCount": 0,
        "progressedSkills": skills,
    }


def test_progressed_skills_skip_chests_and_unstarted_levels():
    skills = progressed_skills_from_course(
        {
            "pathSectioned": [
                {
                    "units": [
                        {
                            "levels": [
                                {
                                    "type": "chest",
                                    "finishedSessions": 1,
                                    "pathLevelClientData": {"skillId": "skip-chest"},
                                },
                                {
                                    "type": "skill",
                                    "finishedSessions": 2,
                                    "pathLevelClientData": {"skillId": "intro"},
                                },
                                {
                                    "type": "skill",
                                    "finishedSessions": 0,
                                    "state": "locked",
                                    "pathLevelClientData": {"skillId": "locked"},
                                },
                                {
                                    "type": "skill",
                                    "finishedSessions": 1,
                                    "pathLevelClientData": {"skillIds": ["one", "two"]},
                                },
                            ]
                        }
                    ]
                }
            ]
        }
    )
    assert [item["skillId"]["id"] for item in skills] == ["intro", "one", "two"]


def test_select_language_course_skips_chess_for_highest_xp_language():
    course = select_language_course(
        {
            "currentCourseId": "CHESS_CH",
            "currentCourse": {"id": "CHESS_CH", "subject": "chess"},
            "courses": [
                {"id": "DUOLINGO_FR_EN", "learningLanguage": "fr", "fromLanguage": "en", "xp": 15089},
                {"id": "DUOLINGO_ES_EN", "learningLanguage": "es", "fromLanguage": "en", "xp": 40536},
            ],
        }
    )
    assert course is not None
    assert course["id"] == "DUOLINGO_ES_EN"


def test_review_queue_uses_learned_lexemes_and_prefers_new_words(monkeypatch):
    from plugins.duolingo import tools

    class Client:
        def get_user_by_id(self, _user_id, fields=None):
            return {
                "id": 1,
                "currentCourseId": "DUOLINGO_FR_EN",
                "fromLanguage": "en",
                "courses": [{"id": "DUOLINGO_FR_EN", "learningLanguage": "fr", "fromLanguage": "en", "xp": 10}],
            }

        def get_course(self, _user_id, _course_id):
            return {
                "id": "DUOLINGO_FR_EN",
                "learningLanguage": "fr",
                "fromLanguage": "en",
                "pathSectioned": [
                    {
                        "units": [
                            {
                                "levels": [
                                    {
                                        "type": "skill",
                                        "finishedSessions": 1,
                                        "pathLevelClientData": {"skillId": "intro"},
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }

        def get_learned_lexemes(self, *_args, **_kwargs):
            return {
                "learnedLexemes": [
                    {"text": "sommeil", "translations": ["sleep"], "isNew": False},
                    {"text": "chut", "translations": ["shush", "shh"], "isNew": True},
                ],
                "pagination": {"totalLexemes": 512},
            }

    monkeypatch.setattr(tools, "_client_or_error", lambda: Client())

    result = json.loads(tools.handle_review_queue({"user_id": 1, "limit": 2}))
    assert result["available_items"] == 512
    assert result["learning_language"] == "fr"
    assert result["review_items"][0]["word"] == "chut"
    assert result["review_items"][0]["is_new"] is True
    assert result["review_items"][0]["translation"] == "shush"


def test_profile_uses_total_lexemes_not_zero_words_learned(monkeypatch):
    from plugins.duolingo import tools

    class Client:
        def get_user(self, _username):
            return {
                "users": [
                    {
                        "username": "learner",
                        "id": 9,
                        "fromLanguage": "en",
                        "currentCourseId": "DUOLINGO_FR_EN",
                        "totalXp": 100,
                        "weeklyXp": 10,
                        "streak": 3,
                        "courses": [
                            {"id": "DUOLINGO_FR_EN", "learningLanguage": "fr", "fromLanguage": "en", "xp": 40}
                        ],
                    }
                ]
            }

        def get_course(self, _user_id, _course_id):
            return {
                "learningLanguage": "fr",
                "fromLanguage": "en",
                "xp": 40,
                "pathSectioned": [],
            }

        def get_learned_lexemes(self, *_args, **_kwargs):
            return {"learnedLexemes": [], "pagination": {"totalLexemes": 512}}

    monkeypatch.setattr(tools, "_client_or_error", lambda: Client())
    result = json.loads(tools.handle_profile({"username": "learner"}))
    assert result["words_learned"] == 512
    assert result["learning_language"] == "fr"
    assert result["course_xp"] == 40


def test_conversation_assessment_uses_learner_turns_only():
    from plugins.duolingo.tools import handle_assess_conversation

    result = json.loads(handle_assess_conversation({
        "transcript": "Tutor: Say casa and perro.\nStudent: Mi casa es grande.",
        "target_vocabulary": ["casa", "perro"],
    }))

    assert result["used"] == ["casa"]
    assert result["not_demonstrated"] == ["perro"]
    assert result["coverage"] == 0.5

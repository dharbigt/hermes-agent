"""Agent-facing learning-data tools for the Duolingo plugin."""

from __future__ import annotations

import re
from typing import Any

from plugins.duolingo.client import (
    DuolingoAPIError,
    DuolingoClient,
    progressed_skills_from_course,
    select_language_course,
)
from plugins.duolingo.practice import (
    FORMS,
    entries_from_learned_lexemes,
    normalize_items,
    practice_brief,
    score_lexemes,
)
from tools.registry import tool_error, tool_result

DUOLINGO_PROFILE_SCHEMA = {
    "name": "duolingo_profile",
    "description": "Read a Duolingo learner's course, XP, streak, and words-learned summary. Read-only.",
    "parameters": {
        "type": "object",
        "properties": {"username": {"type": "string", "description": "Duolingo username."}},
        "required": ["username"],
        "additionalProperties": False,
    },
}

DUOLINGO_REVIEW_QUEUE_SCHEMA = {
    "name": "duolingo_review_queue",
    "description": "Return learned vocabulary from Practice Hub for targeted review. Read-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "minimum": 1, "description": "Duolingo numeric user ID."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum vocabulary items; default 20."},
            "learning_language": {"type": "string", "description": "Optional course language code (e.g. fr). Defaults to the current language course."},
            "from_language": {"type": "string", "description": "Optional UI/from language code (e.g. en)."},
        },
        "required": ["user_id"],
        "additionalProperties": False,
    },
}

DUOLINGO_ASSESS_CONVERSATION_SCHEMA = {
    "name": "duolingo_assess_conversation",
    "description": "Measure which target vocabulary the learner used in a supplied conversation transcript; this reports usage evidence, not language proficiency.",
    "parameters": {
        "type": "object",
        "properties": {
            "transcript": {"type": "string", "description": "Conversation transcript. Prefix learner turns with 'Student:' or 'Learner:' when possible."},
            "target_vocabulary": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100, "description": "Words or phrases being assessed."},
        },
        "required": ["transcript", "target_vocabulary"],
        "additionalProperties": False,
    },
}

DUOLINGO_ASSESS_TEXT_SCHEMA = {
    "name": "duolingo_assess_text",
    "description": "Measure which target vocabulary occurs in any supplied text; usage evidence, not proficiency.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Practice text to score (story, captions, drill, dialogue)."},
            "target_vocabulary": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 100,
                "description": "Words or phrases being assessed.",
            },
        },
        "required": ["text", "target_vocabulary"],
        "additionalProperties": False,
    },
}

DUOLINGO_PRACTICE_BRIEF_SCHEMA = {
    "name": "duolingo_practice_brief",
    "description": "Build a generation brief from a lexeme list or Practice Hub queue. Does not write the text.",
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "minimum": 1, "description": "Duolingo numeric user ID. Pulls the review queue when items are omitted."},
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "description": "Explicit lexemes (strings or {word, translations, is_new} objects). Skips the API when set.",
                "items": {
                    "type": ["string", "object"],
                    "additionalProperties": True,
                },
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Queue size when pulling; default 20."},
            "learning_language": {"type": "string", "description": "Course language code (e.g. vi). Defaults to the current language course when pulling."},
            "from_language": {"type": "string", "description": "UI/from language code (e.g. en)."},
            "form": {
                "type": "string",
                "enum": list(FORMS),
                "description": "narrative, dialogue, captions, drill, or free. Default free.",
            },
            "extra_instructions": {"type": "string", "description": "Optional story hint, tone, or length note for the writer."},
            "exchanges": {"type": "integer", "minimum": 1, "maximum": 40, "description": "Dialogue exchanges, or beats for a narrative."},
        },
        "additionalProperties": False,
    },
}


def check_duolingo_requirements() -> bool:
    try:
        DuolingoClient.from_environment()
    except DuolingoAPIError:
        return False
    return True


def _client_or_error() -> DuolingoClient | str:
    try:
        return DuolingoClient.from_environment()
    except DuolingoAPIError as exc:
        return tool_error(str(exc))


def _first_user(payload: dict[str, Any]) -> dict[str, Any] | None:
    user = (payload.get("users") or [{}])[0] if isinstance(payload.get("users"), list) else payload
    return user if isinstance(user, dict) else None


def _load_language_course(
    client: DuolingoClient,
    user: dict[str, Any],
    user_id: int,
    learning_language: str | None = None,
    from_language: str | None = None,
) -> dict[str, Any]:
    summary = select_language_course(
        user,
        learning_language=learning_language,
        from_language=from_language,
    )
    if not summary or not summary.get("id"):
        raise DuolingoAPIError("No language course found for this learner.")
    course = client.get_course(user_id, str(summary["id"]))
    if not isinstance(course, dict) or not course.get("learningLanguage"):
        raise DuolingoAPIError("Duolingo returned no language course path.")
    return course


def handle_profile(args: dict, **_kwargs: Any) -> str:
    client = _client_or_error()
    if isinstance(client, str):
        return client
    try:
        payload = client.get_user(str(args.get("username") or ""))
    except DuolingoAPIError as exc:
        return tool_error(str(exc))

    user = _first_user(payload)
    if not user:
        return tool_error("Duolingo returned no user profile.")

    summary = select_language_course(user)
    learning_language = user.get("learningLanguage") or (summary or {}).get("learningLanguage")
    from_language = user.get("fromLanguage") or (summary or {}).get("fromLanguage")
    course_xp = (summary or {}).get("xp")
    words_learned = None
    try:
        user_id = int(user.get("id") or 0)
        if user_id and summary:
            course = _load_language_course(client, user, user_id)
            learning_language = course.get("learningLanguage") or learning_language
            from_language = course.get("fromLanguage") or from_language
            if course.get("xp") is not None:
                course_xp = course.get("xp")
            lexemes = client.get_learned_lexemes(
                user_id,
                str(learning_language or ""),
                str(from_language or "en"),
                progressed_skills_from_course(course),
                limit=1,
            )
            raw_pagination = lexemes.get("pagination")
            pagination: dict[str, Any] = raw_pagination if isinstance(raw_pagination, dict) else {}
            if isinstance(pagination.get("totalLexemes"), int):
                words_learned = pagination["totalLexemes"]
    except (TypeError, ValueError, DuolingoAPIError):
        pass

    return tool_result({
        "username": user.get("username"),
        "user_id": user.get("id"),
        "learning_language": learning_language,
        "from_language": from_language,
        "words_learned": words_learned,
        "course_xp": course_xp,
        "total_xp": user.get("totalXp"),
        "weekly_xp": user.get("weeklyXp"),
        "streak": user.get("streak") or (user.get("streakData") or {}).get("length"),
    })


def _review_queue_data(client: DuolingoClient, args: dict) -> dict[str, Any]:
    user_id = int(args.get("user_id") or 0)
    limit = min(100, max(1, int(args.get("limit") or 20)))
    user = client.get_user_by_id(user_id, fields="id,currentCourseId,fromLanguage,courses")
    if not isinstance(user, dict):
        raise DuolingoAPIError("Duolingo returned no user profile.")
    course = _load_language_course(
        client,
        user,
        user_id,
        learning_language=str(args.get("learning_language") or "").strip() or None,
        from_language=str(args.get("from_language") or "").strip() or None,
    )
    learning_language = str(course.get("learningLanguage") or "")
    from_language = str(course.get("fromLanguage") or user.get("fromLanguage") or "en")
    payload = client.get_learned_lexemes(
        user_id,
        learning_language,
        from_language,
        progressed_skills_from_course(course),
        limit=limit,
    )
    entries = entries_from_learned_lexemes(payload)
    raw_pagination = payload.get("pagination")
    pagination: dict[str, Any] = raw_pagination if isinstance(raw_pagination, dict) else {}
    available = pagination.get("totalLexemes")
    return {
        "learning_language": learning_language,
        "from_language": from_language,
        "available_items": available if isinstance(available, int) else len(entries),
        "review_items": entries[:limit],
        "ranking_note": (
            "Newest learned lexemes from Practice Hub; is_new items are listed first. "
            "Duolingo no longer returns per-word strength."
        ),
    }


def handle_review_queue(args: dict, **_kwargs: Any) -> str:
    client = _client_or_error()
    if isinstance(client, str):
        return client
    try:
        return tool_result(_review_queue_data(client, args))
    except (TypeError, ValueError, DuolingoAPIError) as exc:
        return tool_error(str(exc))


def _learner_text(transcript: str) -> str:
    turns = re.findall(r"^(?:student|learner)\s*:\s*(.+)$", transcript, flags=re.IGNORECASE | re.MULTILINE)
    return "\n".join(turns) if turns else transcript


def _targets(raw_targets: Any) -> list[str]:
    if not isinstance(raw_targets, list):
        raise ValueError("target_vocabulary must be a list of words or phrases")
    targets = list(dict.fromkeys(str(item).strip() for item in raw_targets if str(item).strip()))
    if not targets:
        raise ValueError("target_vocabulary must contain at least one word or phrase")
    return targets


def handle_assess_conversation(args: dict, **_kwargs: Any) -> str:
    transcript = str(args.get("transcript") or "").strip()
    if not transcript:
        return tool_error("transcript is required")
    try:
        targets = _targets(args.get("target_vocabulary"))
    except ValueError as exc:
        return tool_error(str(exc))
    result = score_lexemes(_learner_text(transcript), targets)
    result["evidence_scope"] = (
        "Exact vocabulary occurrence in learner-labelled turns when present; this is not a proficiency score."
    )
    return tool_result(result)


def handle_assess_text(args: dict, **_kwargs: Any) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return tool_error("text is required")
    try:
        targets = _targets(args.get("target_vocabulary"))
    except ValueError as exc:
        return tool_error(str(exc))
    result = score_lexemes(text, targets)
    result["evidence_scope"] = "Exact vocabulary occurrence in the supplied text; this is not a proficiency score."
    return tool_result(result)


def handle_practice_brief(args: dict, **_kwargs: Any) -> str:
    raw_items = args.get("items")
    try:
        if raw_items is not None:
            entries = normalize_items(raw_items)
            learning_language = str(args.get("learning_language") or "").strip()
            from_language = str(args.get("from_language") or "").strip()
            available = len(entries)
        else:
            if not args.get("user_id"):
                return tool_error("user_id or items is required")
            client = _client_or_error()
            if isinstance(client, str):
                return client
            queue = _review_queue_data(client, args)
            entries = list(queue["review_items"])
            learning_language = str(args.get("learning_language") or queue.get("learning_language") or "").strip()
            from_language = str(args.get("from_language") or queue.get("from_language") or "").strip()
            available = queue.get("available_items")
            if not isinstance(available, int):
                available = len(entries)
        exchanges = args.get("exchanges")
        brief = practice_brief(
            entries,
            form=str(args.get("form") or "free"),
            learning_language=learning_language,
            from_language=from_language,
            extra_instructions=str(args.get("extra_instructions") or ""),
            exchanges=int(exchanges) if exchanges is not None else None,
            available_items=available,
        )
    except (TypeError, ValueError, DuolingoAPIError) as exc:
        return tool_error(str(exc))
    return tool_result(brief)

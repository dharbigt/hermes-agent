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


def _word_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    word = next((item.get(key) for key in ("text", "word_string", "word", "lexeme_string") if item.get(key)), None)
    if not isinstance(word, str):
        return None
    entry: dict[str, Any] = {"word": word}
    translations = item.get("translations")
    if isinstance(translations, list):
        texts = [text for text in translations if isinstance(text, str) and text]
        if texts:
            entry["translations"] = texts
            entry["translation"] = texts[0]
    if isinstance(item.get("isNew"), bool):
        entry["is_new"] = item["isNew"]
    for key in ("translation", "meaning", "strength", "skill_strength", "learning_progress", "last_practiced"):
        if key in entry:
            continue
        value = item.get(key)
        if isinstance(value, (str, int, float, bool)):
            entry[key] = value
    return entry


def _review_rank(entry: dict[str, Any]) -> tuple[int, str]:
    return (0 if entry.get("is_new") else 1, entry["word"].casefold())


def handle_review_queue(args: dict, **_kwargs: Any) -> str:
    client = _client_or_error()
    if isinstance(client, str):
        return client
    try:
        user_id = int(args.get("user_id") or 0)
        limit = min(100, max(1, int(args.get("limit") or 20)))
        user = client.get_user_by_id(user_id, fields="id,currentCourseId,fromLanguage,courses")
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
    except (TypeError, ValueError, DuolingoAPIError) as exc:
        return tool_error(str(exc))

    items = payload.get("learnedLexemes")
    entries = [entry for item in items if isinstance(item, dict) and (entry := _word_entry(item))] if isinstance(items, list) else []
    entries.sort(key=_review_rank)
    raw_pagination = payload.get("pagination")
    pagination: dict[str, Any] = raw_pagination if isinstance(raw_pagination, dict) else {}
    available = pagination.get("totalLexemes")
    return tool_result({
        "learning_language": learning_language,
        "from_language": from_language,
        "available_items": available if isinstance(available, int) else len(entries),
        "review_items": entries[:limit],
        "ranking_note": "Newest learned lexemes from Practice Hub; is_new items are listed first. Duolingo no longer returns per-word strength.",
    })


def _learner_text(transcript: str) -> str:
    turns = re.findall(r"^(?:student|learner)\s*:\s*(.+)$", transcript, flags=re.IGNORECASE | re.MULTILINE)
    return "\n".join(turns) if turns else transcript


def handle_assess_conversation(args: dict, **_kwargs: Any) -> str:
    transcript = str(args.get("transcript") or "").strip()
    raw_targets = args.get("target_vocabulary")
    if not transcript:
        return tool_error("transcript is required")
    if not isinstance(raw_targets, list):
        return tool_error("target_vocabulary must be a list of words or phrases")
    targets = list(dict.fromkeys(str(item).strip() for item in raw_targets if str(item).strip()))
    if not targets:
        return tool_error("target_vocabulary must contain at least one word or phrase")

    learner_text = _learner_text(transcript)
    used = [word for word in targets if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", learner_text, re.IGNORECASE)]
    missing = [word for word in targets if word not in used]
    return tool_result({
        "target_count": len(targets),
        "used": used,
        "not_demonstrated": missing,
        "coverage": round(len(used) / len(targets), 3),
        "evidence_scope": "Exact vocabulary occurrence in learner-labelled turns when present; this is not a proficiency score.",
    })

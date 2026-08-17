"""Agent-facing learning-data tools for the Duolingo plugin."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from plugins.duolingo.client import DuolingoAPIError, DuolingoClient
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
    "description": "Return vocabulary from a learner's Duolingo overview for targeted review. Read-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "minimum": 1, "description": "Duolingo numeric user ID."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum vocabulary items; default 20."},
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


def handle_profile(args: dict, **_kwargs: Any) -> str:
    client = _client_or_error()
    if isinstance(client, str):
        return client
    try:
        payload = client.get_user(str(args.get("username") or ""))
    except DuolingoAPIError as exc:
        return tool_error(str(exc))

    user = (payload.get("users") or [{}])[0] if isinstance(payload.get("users"), list) else payload
    if not isinstance(user, dict):
        return tool_error("Duolingo returned no user profile.")
    course = user.get("currentCourse") if isinstance(user.get("currentCourse"), dict) else {}
    return tool_result({
        "username": user.get("username"),
        "user_id": user.get("id"),
        "learning_language": user.get("learningLanguage") or course.get("learningLanguage"),
        "from_language": user.get("fromLanguage") or course.get("fromLanguage"),
        "words_learned": course.get("wordsLearned"),
        "course_xp": course.get("xp"),
        "total_xp": user.get("totalXp"),
        "weekly_xp": user.get("weeklyXp"),
        "streak": user.get("streak") or (user.get("streakData") or {}).get("length"),
    })


def _vocabulary_items(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    overview = payload.get("vocab_overview")
    if not isinstance(overview, list):
        return []
    return (item for item in overview if isinstance(item, dict))


def _word_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    word = next((item.get(key) for key in ("word_string", "word", "lexeme_string", "text") if item.get(key)), None)
    if not isinstance(word, str):
        return None
    entry = {"word": word}
    for key in ("translation", "meaning", "strength", "skill_strength", "learning_progress", "last_practiced"):
        value = item.get(key)
        if isinstance(value, (str, int, float, bool)):
            entry[key] = value
    return entry


def _review_rank(entry: dict[str, Any]) -> tuple[int, float, str]:
    for key in ("strength", "skill_strength", "learning_progress"):
        value = entry.get(key)
        if isinstance(value, (int, float)):
            return (0, float(value), entry["word"].casefold())
    return (1, 0.0, entry["word"].casefold())


def handle_review_queue(args: dict, **_kwargs: Any) -> str:
    client = _client_or_error()
    if isinstance(client, str):
        return client
    try:
        payload = client.get_vocabulary_overview(int(args.get("user_id") or 0))
    except (TypeError, ValueError, DuolingoAPIError) as exc:
        return tool_error(str(exc))

    limit = min(100, max(1, int(args.get("limit") or 20)))
    entries = [entry for item in _vocabulary_items(payload) if (entry := _word_entry(item))]
    entries.sort(key=_review_rank)
    return tool_result({
        "learning_language": payload.get("learning_language"),
        "from_language": payload.get("from_language"),
        "available_items": len(entries),
        "review_items": entries[:limit],
        "ranking_note": "Numeric strength fields are ranked lowest first when the API provides them; otherwise items are alphabetical.",
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
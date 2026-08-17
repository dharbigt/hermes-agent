"""Lexeme-deck helpers for practice text. No network, no generation."""

from __future__ import annotations

import re
from typing import Any

FORMS = ("narrative", "dialogue", "captions", "drill", "free")

_FORM_HINTS = {
    "narrative": "Write one short story in the learning language. One scene, beginning to end.",
    "dialogue": (
        "Write one two-person conversation that tells a story. "
        "Number the exchanges. Prefix the learner side with Learner: when there is a clear learner."
    ),
    "captions": "Write standalone example sentences or captions. One target idea per line.",
    "drill": "Write prompt/response or substitution pairs. No plot required.",
    "free": "Follow extra_instructions for the form. Still bind every content word to the lexeme list.",
}


def lexeme_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    word = next(
        (item.get(key) for key in ("text", "word_string", "word", "lexeme_string") if item.get(key)),
        None,
    )
    if not isinstance(word, str):
        return None
    word = word.strip()
    if not word:
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
    elif isinstance(item.get("is_new"), bool):
        entry["is_new"] = item["is_new"]
    for key in ("translation", "meaning", "strength", "skill_strength", "learning_progress", "last_practiced"):
        if key in entry:
            continue
        value = item.get(key)
        if isinstance(value, (str, int, float, bool)):
            entry[key] = value
    return entry


def review_rank(entry: dict[str, Any]) -> tuple[int, str]:
    return (0 if entry.get("is_new") else 1, entry["word"].casefold())


def entries_from_learned_lexemes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("learnedLexemes")
    if not isinstance(items, list):
        return []
    entries = [entry for item in items if isinstance(item, dict) and (entry := lexeme_entry(item))]
    entries.sort(key=review_rank)
    return entries


def normalize_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("items must be a list of words or lexeme objects")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            entry = lexeme_entry({"word": item})
        elif isinstance(item, dict):
            entry = lexeme_entry(item)
        else:
            entry = None
        if not entry:
            continue
        key = entry["word"].casefold()
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    if not entries:
        raise ValueError("items must contain at least one word")
    return entries


def target_words(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["word"] for entry in entries]


def score_lexemes(text: str, targets: list[str]) -> dict[str, Any]:
    used = [
        word
        for word in targets
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, flags=re.IGNORECASE)
    ]
    missing = [word for word in targets if word not in used]
    return {
        "target_count": len(targets),
        "used": used,
        "not_demonstrated": missing,
        "coverage": round(len(used) / len(targets), 3) if targets else 0.0,
    }


def practice_brief(
    entries: list[dict[str, Any]],
    *,
    form: str = "free",
    learning_language: str = "",
    from_language: str = "",
    extra_instructions: str = "",
    exchanges: int | None = None,
    available_items: int | None = None,
) -> dict[str, Any]:
    form = form.strip().casefold() or "free"
    if form not in FORMS:
        raise ValueError(f"form must be one of: {', '.join(FORMS)}")
    if not entries:
        raise ValueError("a lexeme list is required")
    if exchanges is not None and exchanges < 1:
        raise ValueError("exchanges must be a positive integer")

    targets = target_words(entries)
    constraints: dict[str, Any] = {
        "glue_words_ok": True,
        "other_content_words": False,
        "include_gloss": True,
        "mark_targets": True,
        "prefer_all_targets": True,
    }
    if form == "dialogue":
        constraints["exchanges"] = exchanges or 10
        constraints["tell_a_story"] = True
    elif form == "narrative":
        constraints["tell_a_story"] = True
        if exchanges:
            constraints["beats"] = exchanges
    elif exchanges:
        constraints["items"] = exchanges

    extra = extra_instructions.strip()
    instructions = [
        _FORM_HINTS[form],
        "Main text is in the learning language; gloss each utterance or paragraph in the from language.",
        "Use target_vocabulary as the content-word list. Particles, pronouns, and copulas may be added.",
        "Do not introduce other learning-language content words.",
        "Mark target words (e.g. bold). This is practice text, not a proficiency score.",
    ]
    if extra:
        instructions.append(extra)

    return {
        "form": form,
        "learning_language": learning_language or None,
        "from_language": from_language or None,
        "available_items": available_items if isinstance(available_items, int) else len(entries),
        "target_vocabulary": targets,
        "items": entries,
        "constraints": constraints,
        "writer_instructions": instructions,
        "generate": False,
        "note": "This tool does not write the text. The agent writes from this brief, then scores with duolingo_assess_text.",
    }

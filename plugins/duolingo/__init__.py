"""Read-only Duolingo learning-data plugin."""

from plugins.duolingo.tools import (
    DUOLINGO_ASSESS_CONVERSATION_SCHEMA,
    DUOLINGO_ASSESS_TEXT_SCHEMA,
    DUOLINGO_PRACTICE_BRIEF_SCHEMA,
    DUOLINGO_PROFILE_SCHEMA,
    DUOLINGO_REVIEW_QUEUE_SCHEMA,
    check_duolingo_requirements,
    handle_assess_conversation,
    handle_assess_text,
    handle_practice_brief,
    handle_profile,
    handle_review_queue,
)

_TOOLS = (
    ("duolingo_profile", DUOLINGO_PROFILE_SCHEMA, handle_profile, check_duolingo_requirements),
    ("duolingo_review_queue", DUOLINGO_REVIEW_QUEUE_SCHEMA, handle_review_queue, check_duolingo_requirements),
    ("duolingo_practice_brief", DUOLINGO_PRACTICE_BRIEF_SCHEMA, handle_practice_brief, None),
    ("duolingo_assess_text", DUOLINGO_ASSESS_TEXT_SCHEMA, handle_assess_text, None),
    (
        "duolingo_assess_conversation",
        DUOLINGO_ASSESS_CONVERSATION_SCHEMA,
        handle_assess_conversation,
        None,
    ),
)


def register(ctx) -> None:
    """Register the optional Duolingo toolset without modifying Hermes core."""
    for name, schema, handler, check_fn in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="duolingo",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
        )

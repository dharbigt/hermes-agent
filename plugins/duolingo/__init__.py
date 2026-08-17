"""Read-only Duolingo learning-data plugin."""

from plugins.duolingo.tools import (
    DUOLINGO_ASSESS_CONVERSATION_SCHEMA,
    DUOLINGO_PROFILE_SCHEMA,
    DUOLINGO_REVIEW_QUEUE_SCHEMA,
    check_duolingo_requirements,
    handle_assess_conversation,
    handle_profile,
    handle_review_queue,
)

_TOOLS = (
    ("duolingo_profile", DUOLINGO_PROFILE_SCHEMA, handle_profile),
    ("duolingo_review_queue", DUOLINGO_REVIEW_QUEUE_SCHEMA, handle_review_queue),
    (
        "duolingo_assess_conversation",
        DUOLINGO_ASSESS_CONVERSATION_SCHEMA,
        handle_assess_conversation,
    ),
)


def register(ctx) -> None:
    """Register the optional Duolingo toolset without modifying Hermes core."""
    for name, schema, handler in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="duolingo",
            schema=schema,
            handler=handler,
            check_fn=check_duolingo_requirements,
        )
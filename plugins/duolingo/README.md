# Duolingo Plugin

This optional, read-only plugin imports Duolingo profile and vocabulary data to
support targeted vocabulary practice. It uses unofficial endpoints (Practice Hub
`learned-lexemes` plus `/2017-06-30/users`), so endpoint and response-shape
changes are possible.

## Setup

Add a bearer token for the active Hermes profile to `~/.hermes/.env`:

```text
DUOLINGO_BEARER_TOKEN=...
```

Enable the plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - duolingo
```

Restart Hermes and enable the `duolingo` toolset for the platform where the
agent will use it.

## Tools

- `duolingo_profile(username)` returns a deliberately minimized progress
  summary: course language, XP, streak, and words learned
  (`pagination.totalLexemes` from Practice Hub). Chess/math/music current
  courses fall back to the highest-XP language course.
- `duolingo_review_queue(user_id, limit, learning_language?, from_language?)`
  returns learned lexemes from
  `POST /2017-06-30/users/{id}/courses/{learn}/{from}/learned-lexemes`.
  Newest / `is_new` items come first. Per-word strength is no longer provided.
- `duolingo_practice_brief(...)` turns a lexeme list (or a pulled queue) into a
  generation brief: form, target words, constraints, writer instructions. It
  does **not** write the text. Forms: `narrative`, `dialogue`, `captions`,
  `drill`, `free`.
- `duolingo_assess_text(text, target_vocabulary)` reports exact target-word
  use in any practice text.
- `duolingo_assess_conversation(transcript, target_vocabulary)` reports exact
  target-vocabulary use in learner turns. It is an auditable coverage signal,
  not a proficiency judgment.

The plugin never sends practice results or changes a Duolingo account. Do not
provide a Duolingo password; configure only a revocable bearer token.

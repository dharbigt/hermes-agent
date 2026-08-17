# Duolingo Plugin

This optional, read-only plugin imports Duolingo profile and vocabulary data to
support targeted vocabulary practice. It uses the unofficial API documented at
https://github.com/igorskh/duolingo-api, so endpoint and response-shape changes
are possible.

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
  summary: courses, XP, streak, and words learned.
- `duolingo_review_queue(user_id, limit)` returns vocabulary ranked by numeric
  strength when that field is supplied by the API.
- `duolingo_assess_conversation(transcript, target_vocabulary)` reports exact
  target-vocabulary use in learner turns. It is an auditable coverage signal,
  not a proficiency judgment.

The plugin never sends practice results or changes a Duolingo account. Do not
provide a Duolingo password; configure only a revocable bearer token.
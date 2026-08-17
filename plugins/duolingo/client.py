"""Small read-only client for documented Duolingo unofficial API endpoints."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent.secret_scope import get_secret

_BASE_URL = "https://www.duolingo.com"
_USER_AGENT = "Hermes-Duolingo-Plugin/0.1"


class DuolingoAPIError(RuntimeError):
    """An unsuccessful or malformed response from Duolingo."""


@dataclass(frozen=True)
class DuolingoClient:
    """Authenticated client restricted to GET endpoints."""

    bearer_token: str
    base_url: str = _BASE_URL
    timeout_seconds: int = 20

    @classmethod
    def from_environment(cls) -> "DuolingoClient":
        token = (get_secret("DUOLINGO_BEARER_TOKEN", "") or "").strip()
        if not token:
            raise DuolingoAPIError(
                "DUOLINGO_BEARER_TOKEN is not configured in the active Hermes profile."
            )
        return cls(bearer_token=token)

    def get_user(self, username: str) -> dict[str, Any]:
        username = username.strip()
        if not username:
            raise DuolingoAPIError("username is required")
        return self._get("/2017-06-30/users", {"username": username})

    def get_vocabulary_overview(self, user_id: int) -> dict[str, Any]:
        if user_id < 1:
            raise DuolingoAPIError("user_id must be a positive integer")
        return self._get("/vocabulary/overview", {"_": user_id})

    def _get(self, path: str, query: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}?{urlencode(query)}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 -- fixed API host
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise DuolingoAPIError(f"Duolingo returned HTTP {exc.code}.") from exc
        except URLError as exc:
            raise DuolingoAPIError(f"Could not reach Duolingo: {exc.reason}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DuolingoAPIError("Duolingo returned an invalid JSON response.") from exc
        if not isinstance(payload, dict):
            raise DuolingoAPIError("Duolingo returned an unexpected response shape.")
        return payload
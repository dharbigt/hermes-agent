"""Small read-only client for documented Duolingo unofficial API endpoints."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent.secret_scope import get_secret

_BASE_URL = "https://www.duolingo.com"
_USER_AGENT = "Hermes-Duolingo-Plugin/0.2"
_SKIP_LEVEL_TYPES = {"chest", "unit_review"}
_FINISHED_STATES = {"completed", "complete", "golden", "legendary", "passed"}
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")
_NON_LANGUAGE_PREFIXES = ("CHESS_", "MATH_", "MUSIC_")


class DuolingoAPIError(RuntimeError):
    """An unsuccessful or malformed response from Duolingo."""


def _language_code(value: str, label: str) -> str:
    text = value.strip()
    if not _LANG_RE.fullmatch(text):
        raise DuolingoAPIError(f"{label} is not a valid language code.")
    return text


def progressed_skills_from_course(course: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the Practice Hub ``progressedSkills`` body from a course path."""
    skills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section in course.get("pathSectioned") or []:
        if not isinstance(section, dict):
            continue
        for unit in section.get("units") or []:
            if not isinstance(unit, dict):
                continue
            for level in unit.get("levels") or []:
                if not isinstance(level, dict) or level.get("type") in _SKIP_LEVEL_TYPES:
                    continue
                finished = int(level.get("finishedSessions") or 0)
                state = str(level.get("state") or "")
                if finished < 1 and state not in _FINISHED_STATES:
                    continue
                client = level.get("pathLevelClientData")
                client = client if isinstance(client, dict) else {}
                ids: list[Any] = []
                if client.get("skillId"):
                    ids.append(client["skillId"])
                ids.extend(sid for sid in (client.get("skillIds") or []) if sid)
                for skill_id in ids:
                    if not isinstance(skill_id, str) or skill_id in seen:
                        continue
                    seen.add(skill_id)
                    skills.append(
                        {
                            "finishedLevels": 1,
                            "finishedSessions": finished or 1,
                            "skillId": {"id": skill_id},
                        }
                    )
    return skills


def select_language_course(
    user: dict[str, Any],
    learning_language: str | None = None,
    from_language: str | None = None,
) -> dict[str, Any] | None:
    """Pick a language course, skipping chess/math/music current-course traps."""
    courses = [
        course
        for course in (user.get("courses") or [])
        if isinstance(course, dict) and _is_language_course(course)
    ]
    if learning_language:
        wanted = learning_language.casefold()
        courses = [
            course
            for course in courses
            if str(course.get("learningLanguage") or "").casefold() == wanted
        ]
    if from_language:
        wanted_from = from_language.casefold()
        courses = [
            course
            for course in courses
            if str(course.get("fromLanguage") or "").casefold() == wanted_from
        ]

    raw_current = user.get("currentCourse")
    current: dict[str, Any] = raw_current if isinstance(raw_current, dict) else {}
    current_id = user.get("currentCourseId")
    if not learning_language and _is_language_course(current):
        if current.get("id"):
            return current
        if current_id:
            return {**current, "id": current_id}

    for course in courses:
        if course.get("id") == current_id:
            return course
    if not courses:
        return None
    return max(courses, key=lambda course: int(course.get("xp") or 0))


def _is_language_course(course: dict[str, Any]) -> bool:
    course_id = str(course.get("id") or "")
    if any(course_id.startswith(prefix) for prefix in _NON_LANGUAGE_PREFIXES):
        return False
    subject = course.get("subject")
    if subject not in (None, "language"):
        return False
    return bool(course.get("learningLanguage"))


@dataclass(frozen=True)
class DuolingoClient:
    """Authenticated client restricted to read-only user and vocabulary calls."""

    bearer_token: str
    base_url: str = _BASE_URL
    timeout_seconds: int = 40

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
        return self._request("GET", "/2017-06-30/users", {"username": username})

    def get_user_by_id(self, user_id: int, fields: str | None = None) -> dict[str, Any]:
        if user_id < 1:
            raise DuolingoAPIError("user_id must be a positive integer")
        query = {"fields": fields} if fields else None
        return self._request("GET", f"/2017-06-30/users/{user_id}", query)

    def get_course(self, user_id: int, course_id: str) -> dict[str, Any]:
        if user_id < 1:
            raise DuolingoAPIError("user_id must be a positive integer")
        course_id = course_id.strip()
        if not course_id:
            raise DuolingoAPIError("course_id is required")
        return self._request("GET", f"/2017-06-30/users/{user_id}/courses/{course_id}")

    def get_learned_lexemes(
        self,
        user_id: int,
        learning_language: str,
        from_language: str,
        progressed_skills: list[dict[str, Any]],
        *,
        limit: int = 20,
        start_index: int = 0,
        sort_by: str = "LEARNED_DATE",
    ) -> dict[str, Any]:
        if user_id < 1:
            raise DuolingoAPIError("user_id must be a positive integer")
        learn = _language_code(learning_language, "learning_language")
        source = _language_code(from_language, "from_language")
        path = f"/2017-06-30/users/{user_id}/courses/{learn}/{source}/learned-lexemes"
        return self._request(
            "POST",
            path,
            {
                "limit": max(1, min(100, int(limit))),
                "sortBy": sort_by,
                "startIndex": max(0, int(start_index)),
            },
            {
                "lastTotalLexemeCount": 0,
                "progressedSkills": progressed_skills,
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=UTF-8"
            headers["Origin"] = "https://www.duolingo.com"
            headers["Referer"] = "https://www.duolingo.com/practice-hub/words"
        request = Request(url, data=data, headers=headers, method=method)
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

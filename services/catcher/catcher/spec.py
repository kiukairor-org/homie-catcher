"""The JSON contract between Claude and the bot, validated strictly after every run."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

ACTIONS = ("create", "clarify", "choose_repo")
LABELS = ("feature", "bug", "chore")

# Sent to Claude Code via --json-schema (kept to widely supported keywords).
CLAUDE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "title", "user_story", "acceptance_criteria", "labels"],
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "question": {"type": "string", "minLength": 1, "maxLength": 1000},
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "user_story": {"type": "string", "maxLength": 4000},
        "acceptance_criteria": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "labels": {
            "type": "array",
            "maxItems": 3,
            "uniqueItems": True,
            "items": {"type": "string", "enum": list(LABELS)},
        },
        "target_repo": {"type": "string", "maxLength": 100},
        "new_repo": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "description"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 100},
                "description": {"type": "string", "maxLength": 350},
            },
        },
        "duplicate_of": {"type": "string", "maxLength": 200},
    },
}

# What the bot enforces: the above plus "clarify needs a question".
STRICT_SCHEMA: dict[str, Any] = copy.deepcopy(CLAUDE_SCHEMA)
STRICT_SCHEMA["allOf"] = [
    {"if": {"properties": {"action": {"const": "clarify"}}}, "then": {"required": ["question"]}},
]

_VALIDATOR = Draft202012Validator(STRICT_SCHEMA)
Draft202012Validator.check_schema(CLAUDE_SCHEMA)


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class NewRepo:
    name: str
    description: str


@dataclass(frozen=True)
class Spec:
    action: str
    title: str
    user_story: str
    acceptance_criteria: tuple[str, ...]
    labels: tuple[str, ...]
    question: str | None = None
    target_repo: str | None = None
    new_repo: NewRepo | None = None
    duplicate_of: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def validate_spec(data: Any) -> Spec:
    """Validate Claude's structured output. Raises SpecError on anything unexpected."""
    if not isinstance(data, dict):
        raise SpecError("structured output is not a JSON object")
    errors = sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        raise SpecError("; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5]))
    nr = data.get("new_repo")
    return Spec(
        action=data["action"],
        title=data["title"].strip(),
        user_story=data["user_story"].strip(),
        acceptance_criteria=tuple(c.strip() for c in data["acceptance_criteria"]),
        labels=tuple(data["labels"]),
        question=data.get("question"),
        target_repo=data.get("target_repo"),
        new_repo=NewRepo(nr["name"], nr["description"]) if nr else None,
        duplicate_of=data.get("duplicate_of"),
        raw=copy.deepcopy(data),
    )

import pytest

from catcher.spec import CLAUDE_SCHEMA, SpecError, validate_spec
from tests.fakes import spec


def test_valid_create():
    s = validate_spec(spec())
    assert s.action == "create" and s.labels == ("feature",) and len(s.acceptance_criteria) == 2


def test_valid_with_optional_fields():
    s = validate_spec(spec(new_repo={"name": "dark-notes", "description": "Notes"}, duplicate_of="NOTES-3", target_repo="x"))
    assert s.new_repo.name == "dark-notes" and s.duplicate_of == "NOTES-3"


def test_clarify_requires_question():
    with pytest.raises(SpecError):
        validate_spec(spec(action="clarify"))
    assert validate_spec(spec(action="clarify", question="Which page?")).question == "Which page?"


@pytest.mark.parametrize(
    "bad",
    [
        spec(action="delete_repo"),
        spec(labels=["feature", "urgent"]),
        spec(labels=["bug", "bug"]),
        spec(acceptance_criteria="not a list"),
        spec(title=""),
        spec(new_repo={"name": "x"}),
        spec(new_repo={"name": "x", "description": "d", "private": True}),
        {k: v for k, v in spec().items() if k != "title"},
        "just text",
        None,
        [spec()],
    ],
)
def test_invalid(bad):
    with pytest.raises(SpecError):
        validate_spec(bad)


def test_extra_fields_rejected():
    with pytest.raises(SpecError):
        validate_spec(spec(run_command="rm -rf /"))
    with pytest.raises(SpecError):
        validate_spec(spec(actions=[{"github": "DELETE /repos/x"}]))


def test_schema_sent_to_claude_is_closed():
    assert CLAUDE_SCHEMA["additionalProperties"] is False
    assert set(CLAUDE_SCHEMA["properties"]["action"]["enum"]) == {"create", "clarify", "choose_repo"}

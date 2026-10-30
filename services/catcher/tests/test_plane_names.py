import re

from catcher.plane import identifier_for, project_name_for

# Copied from Plane CE v1.4.2 plane/db/models/project.py
FORBIDDEN = r"^.*[&+,:;$^}{*=?@#|'<>.()%!-].*$"


def test_project_name_is_plane_safe():
    for repo in ["agentic-demo", "my.app", "a-b-c", "plain"]:
        assert not re.match(FORBIDDEN, project_name_for(repo)), repo
    assert project_name_for("agentic-demo") == "agentic_demo"


def test_identifier_is_alnum():
    assert identifier_for("agentic-demo") == "AGENTICDEMO"

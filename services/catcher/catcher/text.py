"""Rendering model output (derived from untrusted chat text) safely."""

from __future__ import annotations

import html
import re

from .spec import Spec

ZWSP = "​"


def defang(text: str) -> str:
    """Neutralise @-mentions (no @channel/@all/@someone pings) and HTML comments/tags."""
    text = re.sub(r"@(?=[A-Za-z0-9_.-])", "@" + ZWSP, text)
    return text.replace("<", "&lt;").replace(">", "&gt;")


def spec_markdown(spec: Spec) -> str:
    lines = [f"**{defang(spec.title)}**", "", defang(spec.user_story), "", "**Acceptance criteria**"]
    lines += [f"- {defang(c)}" for c in spec.acceptance_criteria]
    if spec.labels:
        lines += ["", "Labels: " + ", ".join(spec.labels)]
    if spec.duplicate_of:
        lines += ["", f"Possible duplicate of: {defang(spec.duplicate_of)}"]
    return "\n".join(lines)


def spec_html(spec: Spec, idea: str, source: str = "Original idea (Mattermost)") -> str:
    e = html.escape
    crit = "".join(f"<li>{e(c)}</li>" for c in spec.acceptance_criteria)
    dup = f"<p>Possible duplicate of: {e(spec.duplicate_of)}</p>" if spec.duplicate_of else ""
    return (
        f"<p>{e(spec.user_story)}</p><h3>Acceptance criteria</h3><ul>{crit}</ul>{dup}<h3>{e(source)}</h3><blockquote>{e(idea)}</blockquote>"
    )


def issue_body(spec: Spec, plane_url: str, root_post_id: str, plane_item_id: str, *, new_app: str | None = None) -> str:
    lines = []
    if new_app:
        lines += [f"Build v1 of this app (`{new_app}`), starting from the app-template scaffold.", ""]
    lines += ["## User story", "", defang(spec.user_story), "", "## Acceptance criteria", ""]
    lines += [f"- [ ] {defang(c)}" for c in spec.acceptance_criteria]
    lines += ["", f"Plane: {plane_url}", ""]
    # Ids are ours (Mattermost/Plane ids), not user text.
    lines.append(f"<!-- homie-catcher thread={root_post_id} plane={plane_item_id} -->")
    return "\n".join(lines)


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|~>-])")


def md_escape(text: str) -> str:
    """Escape Markdown and neutralise mentions/HTML in untrusted text (alerts), single line."""
    text = " ".join(str(text).split())  # no newlines: can't start headings/lists/quotes
    return defang(_MD_SPECIAL.sub(r"\\\1", text))


def clip(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"

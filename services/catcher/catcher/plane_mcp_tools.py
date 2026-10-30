"""Plane MCP tool names Claude may call (read-only), for the pinned plane-mcp-server version.

Source: plane-mcp-server ==0.2.10 from PyPI (makeplane/plane-mcp-server). Every tool there is
registered with a bare ``@mcp.tool()``, so the tool name is the Python function name.
``ALL_TOOLS`` is the full catalogue as advertised over stdio by ``tools/list`` for that
version (enumerated 2026-10-30 with an MCP client). Claude Code exposes them as
``mcp__<server>__<tool>``.

Only a small subset needed to spot duplicates is allowlisted; everything else in the
catalogue is passed to ``--disallowedTools`` as well (deny rules win over allow rules).
Deliberately NOT allowed even though read-only: tools that fetch URLs
(``get_work_item_attachment_download_url``, ``read_work_item_attachment``) and tools that
return people data (``get_me``, ``get_workspace_members``, ``get_project_members``).
"""

PLANE_MCP_VERSION = "0.2.10"
PLANE_MCP_SERVER_NAME = "plane"

ALL_TOOLS: frozenset[str] = frozenset(
    {
        "attach_page_to_work_item",
        "complete_cycle",
        "count_work_items",
        "create_cycle",
        "create_initiative",
        "create_intake_work_item",
        "create_label",
        "create_milestone",
        "create_module",
        "create_page",
        "create_project",
        "create_project_estimate",
        "create_project_estimate_points",
        "create_state",
        "create_work_item",
        "create_work_item_comment",
        "create_work_item_link",
        "create_work_item_property",
        "create_work_item_property_option",
        "create_work_item_relation",
        "create_work_item_relation_definition",
        "create_work_item_type",
        "create_work_log",
        "delete_cycle",
        "delete_initiative",
        "delete_intake_work_item",
        "delete_label",
        "delete_milestone",
        "delete_module",
        "delete_project",
        "delete_project_estimate",
        "delete_project_estimate_point",
        "delete_state",
        "delete_work_item",
        "delete_work_item_attachment",
        "delete_work_item_comment",
        "delete_work_item_link",
        "delete_work_item_property",
        "delete_work_item_property_option",
        "delete_work_item_property_value",
        "delete_work_item_relation_definition",
        "delete_work_item_type",
        "delete_work_log",
        "detach_page_from_work_item",
        "get_features",
        "get_me",
        "get_pql_reference",
        "get_project_estimate",
        "get_project_members",
        "get_project_worklog_summary",
        "get_work_item_attachment_download_url",
        "get_work_item_property_value",
        "get_workspace_members",
        "import_work_item_types_to_project",
        "link_estimate_to_project",
        "list_archived_work_items",
        "list_cycle_work_items",
        "list_cycles",
        "list_initiative_projects",
        "list_initiatives",
        "list_intake_work_items",
        "list_labels",
        "list_milestone_work_items",
        "list_milestones",
        "list_module_work_items",
        "list_modules",
        "list_pages",
        "list_project_estimate_points",
        "list_projects",
        "list_roles",
        "list_states",
        "list_work_item_activities",
        "list_work_item_attachments",
        "list_work_item_comments",
        "list_work_item_links",
        "list_work_item_pages",
        "list_work_item_properties",
        "list_work_item_property_options",
        "list_work_item_relation_definitions",
        "list_work_item_relations",
        "list_work_item_types",
        "list_work_items",
        "list_work_logs",
        "manage_cycle_archive",
        "manage_cycle_work_items",
        "manage_initiative_projects",
        "manage_milestone_work_items",
        "manage_module_archive",
        "manage_module_work_items",
        "manage_project_archive",
        "manage_work_item_archive",
        "manage_work_item_assignee",
        "manage_work_item_label",
        "manage_work_item_type_properties",
        "read_work_item_attachment",
        "remove_work_item_relation",
        "resolve_work_item_type",
        "retrieve_cycle",
        "retrieve_initiative",
        "retrieve_intake_work_item",
        "retrieve_label",
        "retrieve_milestone",
        "retrieve_module",
        "retrieve_page",
        "retrieve_project",
        "retrieve_role",
        "retrieve_state",
        "retrieve_work_item",
        "retrieve_work_item_activity",
        "retrieve_work_item_by_identifier",
        "retrieve_work_item_comment",
        "retrieve_work_item_link",
        "retrieve_work_item_property",
        "retrieve_work_item_property_option",
        "retrieve_work_item_type",
        "search_work_items",
        "set_work_item_property_value",
        "transfer_cycle_work_items",
        "update_cycle",
        "update_initiative",
        "update_intake_work_item",
        "update_label",
        "update_milestone",
        "update_module",
        "update_project",
        "update_project_estimate",
        "update_project_estimate_point",
        "update_project_features",
        "update_state",
        "update_work_item",
        "update_work_item_comment",
        "update_work_item_link",
        "update_work_item_property",
        "update_work_item_property_option",
        "update_work_item_relation_definition",
        "update_work_item_type",
        "update_work_log",
        "update_workspace_features",
        "upload_work_item_attachment_from_url",
    }
)

READ_ONLY_TOOLS: tuple[str, ...] = (
    "list_projects",
    "retrieve_project",
    "list_states",
    "list_labels",
    "list_work_items",
    "retrieve_work_item",
    "retrieve_work_item_by_identifier",
    "search_work_items",
    "list_work_item_comments",
    "get_pql_reference",
)

# Substrings that mark a tool as able to change something. A test asserts that no
# allowlisted name contains any of them.
WRITE_MARKERS: tuple[str, ...] = (
    "create",
    "update",
    "delete",
    "add",
    "remove",
    "archive",
    "transfer",
    "manage",
    "upload",
    "attach",
    "detach",
    "set_",
    "import",
    "link_",
    "complete",
    "resolve",
)


def mcp_name(tool: str) -> str:
    return f"mcp__{PLANE_MCP_SERVER_NAME}__{tool}"


def allowed_tool_names() -> list[str]:
    return [mcp_name(t) for t in READ_ONLY_TOOLS]


def denied_tool_names() -> list[str]:
    return [mcp_name(t) for t in sorted(ALL_TOOLS - set(READ_ONLY_TOOLS))]

#!/usr/bin/env python3
"""Generate the editor-agent (VS Code / Cursor) MCP configuration and gate policy from catalog.yml.

Outputs
  agent/aap_mcp_policy.json   policy read by tools/aap_mcp_gate.py (tool allowlist, launchable
                              templates and workflows with their survey variables)
  .mcp.json                   VS Code (Copilot harness) workspace MCP servers, portable format
  .cursor/mcp.json            Cursor workspace MCP servers

The MCP server connections never use the AAP "discover" endpoint. Its generic call_tool tool
would let the agent reach any tool in any toolset and get around the per-tool allowlist.

Usage: python3 tools/gen_agent_config.py [--check]
"""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_config  # noqa: E402  (reuse the exact workflow survey that gen_config generates)

ROOT = Path(__file__).resolve().parent.parent

TOKEN_ENV = "AAP_MCP_TOKEN"
URL_ENV = "AAP_MCP_URL"            # e.g. https://aap.example.com:8448  (no trailing /mcp)
CHANGE_NUMBER_REGEX = r"^CHG[0-9]{7,}$"

# Read-only tools the agent may call without a prompt, grouped by MCP server (= AAP toolset).
# Names are the tool names the AAP MCP server publishes (OpenAPI operationId).
READ_TOOLS = {
    "aap-jobs": [
        "job_templates_list", "job_templates_retrieve", "job_templates_launch_retrieve",
        "workflow_job_templates_list", "workflow_job_templates_retrieve", "unified_job_templates_list",
        "jobs_list", "jobs_retrieve", "jobs_stdout_retrieve", "jobs_job_events_list",
        "jobs_job_host_summaries_list", "workflow_jobs_list", "workflow_jobs_retrieve",
        "workflow_jobs_workflow_nodes_list", "schedules_list", "schedules_retrieve", "projects_list",
        "activation_instances_list", "activation_instances_logs_list",
    ],
    "aap-monitor": [
        "status_retrieve", "feature_flags_state_retrieve", "instance_groups_list", "instance_groups_retrieve",
        "instances_retrieve", "mesh_visualizer_retrieve", "activity_stream_list", "activity_stream_retrieve",
        "activitystream_list", "activitystream_retrieve",
    ],
    "aap-inventory": [
        "inventories_list", "inventory_sources_list", "inventories_inventory_sources_list", "groups_list",
        "hosts_list", "hosts_retrieve", "hosts_variable_data_retrieve", "hosts_ansible_facts_retrieve",
    ],
}
# Launch tools: the gate checks the template, the survey answers and the limit, then decides.
LAUNCH_TOOLS = {"aap-jobs": ["job_templates_launch_create", "workflow_job_templates_launch_create"]}
TOOLSETS = {"aap-jobs": "job_management", "aap-monitor": "system_monitoring", "aap-inventory": "inventory_management"}

# Tools that exist on these toolsets but are refused even if a client shows them. Each one has
# a reason the agent can act on. Anything not listed anywhere is refused as "not on the allowlist".
DENY_REASONS = {
    "call_tool": "The AAP discover endpoint is not allowed: call_tool bypasses the per-tool allowlist. "
                 "Connect the per-toolset endpoints in .mcp.json / .cursor/mcp.json instead.",
    "discover": "The AAP discover endpoint is not allowed. Use the aap-jobs, aap-monitor and aap-inventory servers.",
    "jobs_cancel_create": "Cancelling jobs goes through approval. Launch 'WF - JOB - Cancel stuck jobs'.",
    "workflow_jobs_cancel_create": "Cancelling workflows goes through approval. Launch 'WF - JOB - Cancel stuck jobs', "
                                   "or ask an AAP Ops Approver to deny the pending approval.",
    "jobs_relaunch_create": "Relaunching goes through approval. Launch 'WF - JOB - Relaunch failed job'.",
    "workflow_jobs_relaunch_create": "Relaunching goes through approval. Launch the WF workflow again with a valid change.",
    "schedules_create": "Schedules are managed as config-as-code. Use 'WF - JOB - Enable or disable schedules'.",
    "schedules_update": "Schedules are managed as config-as-code. Use 'WF - JOB - Enable or disable schedules'.",
    "schedules_destroy": "Schedules are managed as config-as-code. Use 'WF - JOB - Enable or disable schedules'.",
    "job_templates_create": "Job templates are managed as config-as-code (catalog.yml + 'WF - PLAT - Apply config-as-code').",
    "job_templates_survey_spec_create": "Surveys are managed as config-as-code (catalog.yml).",
    "projects_create": "Projects are managed as config-as-code.",
    "ad_hoc_commands_create": "Ad hoc commands are not allowed for agents. Use a catalog job template.",
    "hosts_ad_hoc_commands_create": "Ad hoc commands are not allowed for agents. Use a catalog job template.",
    "inventories_ad_hoc_commands_create": "Ad hoc commands are not allowed for agents. Use a catalog job template.",
    "groups_ad_hoc_commands_create": "Ad hoc commands are not allowed for agents. Use a catalog job template.",
    "ad_hoc_commands_relaunch_create": "Ad hoc commands are not allowed for agents.",
    "hosts_create": "Inventory changes are not allowed for agents.",
    "groups_create": "Inventory changes are not allowed for agents.",
    "inventories_create": "Inventory changes are not allowed for agents.",
    "inventory_sources_create": "Inventory changes are not allowed for agents.",
    "inventory_sources_update_create": "Inventory source syncs are not allowed for agents.",
    "instances_create": "Instance changes are not allowed for agents.",
    "instance_groups_create": "Instance group changes are not allowed for agents.",
}

# Shell commands an editor agent must not run, because they would reach AAP outside the MCP gate.
# SHELL_MATCHER is a cheap pre-filter (Cursor hook matcher); the gate then applies these rules:
#   - any reference to an AAP token variable
#   - an HTTP client (curl, wget, httpie, Invoke-RestMethod, python requests/urllib) pointed at an AAP API or MCP path
#   - the awx CLI, ansible-navigator/ansible-playbook against aap_config/configure_aap.yml
SHELL_MATCHER = r"AAP_|CONTROLLER_|/api/|/mcp/|awx|configure_aap"
SHELL_TOKEN_REGEX = r"\b(AAP_MCP_TOKEN|AAP_TOKEN|CONTROLLER_OAUTH_TOKEN|CONTROLLER_PASSWORD)\b"
SHELL_CLIENT_REGEX = r"(^|[\s;&|(])(curl|wget|http|https|xh|Invoke-RestMethod|Invoke-WebRequest|irm|iwr)(\s|$)|requests\.|urllib|httpx"
SHELL_AAP_PATH_REGEX = r"/api/(controller|gateway|eda|galaxy)/|/api/v2/|/mcp/[a-z_]+"
SHELL_CLI_REGEX = r"(^|[\s;&|(])awx(\s|$)|configure_aap\.yml"

def build(cat):
    by_id = {t["id"]: t for t in cat["templates"]}
    job_templates, workflows = {}, {}
    for t in cat["templates"]:
        hosts = t["target"] == "hosts"
        if t["changes"]:
            wf = gen_config.workflow(cat, t, by_id)
            workflows[wf["name"]] = {
                "template": t["name"], "target": t["target"], "requires_limit": hosts,
                "survey_vars": [q["variable"] for q in wf["survey_spec"]["spec"]],
                "required_vars": [q["variable"] for q in wf["survey_spec"]["spec"] if q["required"]],
            }
        elif t["id"] not in gen_config.WF_ONLY:
            job_templates[t["name"]] = {
                "kind": gen_config.kind(t), "target": t["target"], "requires_limit": hosts,
                "survey_vars": [s["var"] for s in t.get("survey", [])],
                "required_vars": [s["var"] for s in t.get("survey", []) if s.get("required")],
            }
    blocked = sorted([t["name"] for t in cat["templates"] if t["changes"] or t["id"] in gen_config.WF_ONLY])
    policy = {
        "_generated": "tools/gen_agent_config.py from catalog.yml - do not edit by hand",
        "version": 1,
        "servers": {s: {"toolset": TOOLSETS[s]} for s in TOOLSETS},
        "read_tools": READ_TOOLS,
        "launch_tools": LAUNCH_TOOLS,
        "deny_reasons": DENY_REASONS,
        "launch_body_keys": ["limit", "extra_vars"],
        "broad_limits": ["all", "*", "*:*", "&all", "!none"],
        "change_number_regex": CHANGE_NUMBER_REGEX,
        "shell": {"matcher": SHELL_MATCHER, "token_regex": SHELL_TOKEN_REGEX, "client_regex": SHELL_CLIENT_REGEX,
                  "aap_path_regex": SHELL_AAP_PATH_REGEX, "cli_regex": SHELL_CLI_REGEX},
        "agent_team": gen_config.TEAM_AGENTS,
        "approver_team": gen_config.TEAM_APPROVERS,
        "id_map_file": ".aap-mcp/template_ids.json",
        "id_map_max_age_days": 7,
        "audit_log": ".aap-mcp/audit.jsonl",
        "job_templates": job_templates,
        "workflows": workflows,
        "blocked_templates": blocked,
    }

    def servers(fmt):
        out = {}
        for s, toolset in TOOLSETS.items():
            if fmt == "cursor":
                url, auth = f"${{env:{URL_ENV}}}/mcp/{toolset}", f"Bearer ${{env:{TOKEN_ENV}}}"
            else:
                url, auth = f"${{{URL_ENV}}}/mcp/{toolset}", f"Bearer ${{{TOKEN_ENV}}}"
            entry = {"type": "http", "url": url, "headers": {"Authorization": auth}}
            if fmt == "vscode":
                entry["tools"] = READ_TOOLS[s] + LAUNCH_TOOLS.get(s, [])
            out[s] = entry
        return {"mcpServers": out}

    dump = lambda o: json.dumps(o, indent=2) + "\n"  # noqa: E731
    return {
        ROOT / "agent" / "aap_mcp_policy.json": dump(policy),
        ROOT / ".mcp.json": dump(servers("vscode")),
        ROOT / ".cursor" / "mcp.json": dump(servers("cursor")),
    }


def main():
    check = "--check" in sys.argv
    cat = yaml.safe_load((ROOT / "catalog.yml").read_text())
    stale = []
    for path, body in build(cat).items():
        if check:
            if not path.exists() or path.read_text() != body:
                stale.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    if stale:
        print("stale:\n" + "\n".join(stale))
        sys.exit(1)
    print("agent config up to date" if check else "agent config generated")


if __name__ == "__main__":
    main()

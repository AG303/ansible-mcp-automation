# MCP Agent Guide

How an AI agent connected to the AAP 2.6 MCP server should use this content. You can paste the
"Agent instructions" section straight into the agent's system prompt.

## Connecting

- Turn on the MCP server. On the OpenShift operator, set `spec.mcp.disabled: false` on the
  `AnsibleAutomationPlatform` CR, or run `WF - PLAT - Enable or disable MCP server`. When it's up, a route ending in
  `-mcp` appears ([Red Hat: Enable the MCP server on OCP](https://docs.redhat.com/it/documentation/red_hat_ansible_automation_platform/2.6/install-enable_the_mcp_server_on_an_existing_deployment)).
- Authenticate with a personal access token belonging to a user in the `MCP Agents` team
  ([Red Hat: Authenticate with the MCP server](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/extend-authenticate_with_the_mcp_server)).
- Turn on the `job_management` toolset, which covers job templates, jobs and workflow jobs. Launching needs write
  operations (`ALLOW_WRITE_OPERATIONS=true`)
  ([MCP configuration reference](https://docs.redhat.com/es/documentation/red_hat_ansible_automation_platform/2.6/extend-standalone_mcp_server_configuration_reference)).
  Optionally add `system_monitoring` for instance and status data. RBAC means the agent's token can only execute
  what the `MCP Agents` team was granted (see [GUARDRAILS.md](GUARDRAILS.md)).
- Connect each toolset at its own URL (`https://<host>:8448/mcp/job_management`, and so on). Don't connect
  `/mcp/discover`: its `call_tool` tool reaches every tool the token can use, so a client-side tool allowlist or
  per-tool approval can't see what actually runs.
- For VS Code (GitHub Copilot) and Cursor, this repo ships ready-made MCP configs, an approval-gate hook and the agent
  instructions below. See [EDITOR_AGENTS.md](EDITOR_AGENTS.md).

## Naming contract

| Prefix | Meaning | Agent action |
|---|---|---|
| `MON -`, `JOB -`, `SEC -`, `PLAT -`, `PATCH -` with description starting `Read-only.` | Safe and read-only | Launch the job template directly |
| `SN -` with description starting `Writes ServiceNow records only.` | Creates or updates ServiceNow records; does not change systems | Launch directly |
| `WF - <name>` | Wraps a template that CHANGES SYSTEMS: ServiceNow change gate, then human approval, then run | Launch the workflow (requires `sn_change_number`) |
| Description contains `Use the WF workflow` | The bare job template; the agent cannot execute it | Never launch it; use `WF - <same name>` |

Labels make filtering easy: `mcp-agent`, `read-only`, `changing`, `servicenow-write`, `approval-required`, plus one
domain label (`server-monitoring`, `job-management`, `security-compliance`, `platform-configuration`,
`patch-management`, `servicenow-incident`).

## Launch parameters

- `limit`: every host template asks for a limit at launch. Always set it to the narrowest host pattern that fits.
  When you leave it out, the template runs against the whole `AAP Ops - Managed Hosts` inventory.
- `extra_vars`: these are survey answers only. Anything outside the survey gets rejected (`ask_variables_on_launch: false`; only the six workflow-node templates accept free-form vars).
  The survey variable names are listed in [CATALOG.md](CATALOG.md).
- Workflows: put `sn_change_number` first, then the wrapped template's survey. The change has to be approved, in the
  Scheduled or Implement state, and inside its planned window, and its CIs have to cover the `limit` hosts. Otherwise
  the gate fails before anything runs.

## Reading results

Every job publishes one artifact, `aap_ops_result`, and also prints the same summary in the job output:

```json
{
  "aap_ops_result": {
    "template": "MON - Disk usage and largest directories",
    "job_id": 4211,
    "generated": "2026-11-05T15:04:11Z",
    "overall": "warn",
    "counts": {"ok": 11, "warn": 1, "fail": 0, "changed": 0, "skipped": 0},
    "hosts": [
      {"host": "web03", "status": "warn", "summary": "/var 91% used (warn 85%)", "details": {"...": "..."}}
    ]
  }
}
```

- `status` values: `ok`, `warn`, `fail`, `changed`, `skipped` (unsupported OS).
- The job fails when any host has a status in `aap_ops_fail_on_status` (default `[fail]`). A failed job therefore means
  "findings", and the artifact explains why. Unreachable hosts show up as `fail` and keep their error message.
- `hosts` is sorted by status and capped at 200 entries. Each `details` value is cut to 2000 characters. The full data is
  in the JSON and HTML reports under `aap_ops_report_dir` on the execution node.
- Workflows: read the artifact from the main node's job. The gate node's artifact lists each gate check that passed or failed.

## Common intents

| User asks | Template |
|---|---|
| "Is web01 healthy?" | `MON - Host health check` (limit web01) |
| "What filled the disk on db02?" | `MON - Disk usage and largest directories` |
| "Restart nginx on web01" | `WF - MON - Restart service with validation` (needs CHG) |
| "Which jobs failed overnight?" | `JOB - Failed jobs summary` |
| "Something is stuck in the queue" | `JOB - Queue and capacity status`, then `WF - JOB - Cancel stuck jobs` |
| "Are we CIS compliant?" | `SEC - Compliance scan (CIS or STIG)` with `compliance_profile=cis` |
| "Harden SSH on the new hosts" | `WF - SEC - SSH hardening` |
| "Is the platform healthy / is MCP up?" | `PLAT - AAP platform health`, `PLAT - MCP server status` |
| "Did config drift from Git?" | `PLAT - Configuration drift check` |
| "What patches are pending?" | `PATCH - Available updates`, `PATCH - Patch compliance report` |
| "Are we exposed to CVE-XXXX in openssl?" | `PATCH - Package or CVE exposure check` |
| "Patch the app tier tonight" | `SN - Create change request`, wait for CAB approval, then `WF - PATCH - Apply updates` |
| "Look into INC0012345" | `SN - Incident diagnostics` |
| "Fix INC0012345" | `WF - SN - Incident remediation` (needs CHG) |
| "Why does this keep happening?" | `SN - Problem candidates from incident trend`, `SN - Problem root cause evidence` |

## Agent instructions

```
You operate Ansible Automation Platform through its MCP server.
1. Discover: list job templates and workflow job templates with label "mcp-agent". Read the description.
2. Prefer read-only templates. Launch them directly with a narrow limit and only survey variables.
3. Never launch a job template whose description says "Use the WF workflow". Use "WF - <same name>".
4. A WF workflow needs an approved ServiceNow change number (sn_change_number). If the user has none,
   offer to run "SN - Create change request" and explain that a CAB/ServiceNow approval and an AAP
   approval are both required before anything changes.
5. After launch, poll the job or workflow job until it finishes, then read artifacts.aap_ops_result.
   Report overall, counts, and the hosts with warn/fail and their summary. A failed job with an
   aap_ops_result is a finding, not a platform error.
6. A workflow waiting on approval is normal. Tell the user who must approve ("AAP Ops Approvers").
7. Do not retry a failed changing workflow automatically. The failure path already closed the change
   as unsuccessful and opened an incident; report both numbers.
```

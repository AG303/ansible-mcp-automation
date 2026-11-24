# Editor Agents: VS Code and Cursor

This sets up the AAP 2.6 MCP server for the agent in VS Code (GitHub Copilot) and Cursor, with an approval gate on
every AAP tool call. Everything lives in this repo, so opening the folder in either editor picks it up.

| File | Editor | Purpose |
|---|---|---|
| `.mcp.json` | VS Code | Three MCP servers (`aap-jobs`, `aap-monitor`, `aap-inventory`) with a per-server tool allowlist |
| `.cursor/mcp.json` | Cursor | The same three servers |
| `.github/hooks/aap-mcp-gate.json` | VS Code | `preToolUse` hook that runs the gate |
| `.cursor/hooks.json` | Cursor | `beforeMCPExecution` and `beforeShellExecution` hooks that run the gate, `failClosed: true` |
| `.github/agents/aap-operator.agent.md` | VS Code | "AAP Operator" custom agent: AAP tools plus read/search, no terminal or edit tools |
| `.cursor/rules/aap-mcp.mdc` | Cursor | Always-on rule with the operating instructions |
| `.vscode/settings.json` | VS Code | No global auto-approve; interactive mode with manual approvals |
| `agent/aap_mcp_policy.json` | both | Gate policy, generated from `catalog.yml` |
| `tools/aap_mcp_gate.py` | both | The gate (Python 3 standard library only) |
| `tools/aap_mcp_sync.py` | both | Preflight of the agent token plus the template ID map the gate needs |

`tools/gen_agent_config.py` generates `.mcp.json`, `.cursor/mcp.json` and `agent/aap_mcp_policy.json` from the
catalog. Run it after any catalog change; `--check` fails when they're stale.

## What gets approved where

| Agent call | Gate decision | Then |
|---|---|---|
| Read tools (list/retrieve jobs, templates, stdout, events, host summaries, inventories, hosts, status, instance groups, activity stream) | allow | Runs |
| Launch a read-only catalog job template with a narrow `limit` and only survey answers | allow | Runs |
| Launch an `SN -` template that writes ServiceNow records | ask | You confirm in the editor |
| Launch a `WF -` workflow with a valid `sn_change_number` | ask | You confirm; AAP runs the ServiceNow change gate, then waits for a human in `AAP Ops Approvers` |
| Launch a bare changing template, an unknown ID, `limit` empty/`all`/`*`, an unasked survey variable, or launch-time overrides (inventory, credentials, job_type, tags) | deny | The agent gets the reason and the fix |
| Cancel or relaunch, create/update/delete objects, ad hoc commands, anything outside the allowlist | deny | Cancel goes through `WF - JOB - Cancel stuck jobs`; objects change through config as code |
| The `/mcp/discover` endpoint, its `call_tool` tool, or an AAP MCP URL under any other server name | deny | `call_tool` would bypass the editor's tool allowlist |
| Shell commands that use an AAP token variable, call the AAP API or MCP URL with curl/wget/Python, run the `awx` CLI, or apply `configure_aap.yml` | deny | The agent has to use the gated MCP tools |
| Other MCP servers and tools | Cursor: ask (set `AAP_MCP_GATE_PASSTHROUGH`); VS Code: no decision, so normal approvals apply | |

AAP RBAC stays the hard boundary. The gate is a second, earlier check that gives the agent useful errors and keeps
the human in the loop. See [GUARDRAILS.md](GUARDRAILS.md#7-editor-agent-gate).

Every gate decision is appended to `.aap-mcp/audit.jsonl` with the tool, template, limit and change number. Survey
values aren't logged. `.aap-mcp/` is git-ignored.

## Setup

1. Turn on the MCP server with the `job_management`, `system_monitoring` and `inventory_management` toolsets and
   `ALLOW_WRITE_OPERATIONS=true`, so the two launch tools exist. Leave the gate to decide which launches go through
   ([MCP configuration reference](https://docs.redhat.com/es/documentation/red_hat_ansible_automation_platform/2.6/extend-standalone_mcp_server_configuration_reference)).
   Each toolset has its own URL, `https://<host>:8448/mcp/<toolset>`, and the client authenticates with
   `Authorization: Bearer <token>`
   ([Red Hat: Connect an AI agent to the MCP server](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/extend-proc_connect_ai_agent_ansible_mcp_server)).
2. Create a dedicated AAP user for the agent. Put it in `MCP Agents` only: not a superuser, not an org admin, not in
   `AAP Ops Approvers`. Issue a personal access token for that user.
3. Export the variables in the shell that starts the editor (or your login profile), never in a committed file:
   ```bash
   export AAP_URL=https://aap.example.com            # platform gateway
   export AAP_MCP_URL=https://aap.example.com:8448   # MCP server, no trailing slash
   export AAP_MCP_TOKEN=<token of the agent user>
   ```
   VS Code reads `${AAP_MCP_URL}` and `${AAP_MCP_TOKEN}` in `.mcp.json`; Cursor reads `${env:AAP_MCP_URL}` and
   `${env:AAP_MCP_TOKEN}` in `.cursor/mcp.json`
   ([VS Code MCP configuration](https://code.visualstudio.com/docs/agents/reference/mcp-configuration),
   [Cursor MCP](https://cursor.com/docs/context/mcp)). Launch the editor from that shell (`code .` or `cursor .`) so it inherits them.
4. Run the preflight in your own terminal:
   ```bash
   python3 tools/aap_mcp_sync.py
   ```
   It fails, and writes nothing, if the token user is a superuser or an approver, if the token can start any changing
   or workflow-only job template directly, or if a `WF -` workflow has lost its approval node. Otherwise it writes
   `.aap-mcp/template_ids.json` (ID to template name). The gate denies every launch until this file exists and asks
   for confirmation on read-only launches when it's more than 7 days old. Re-run it after applying `aap_config`. Use
   `--check-only` to check without writing, `--insecure` only in a lab.
5. Open the folder, trust the workspace, and start the servers.
   - VS Code: hooks in `.github/hooks/*.json` load for trusted workspaces
     ([VS Code hooks](https://code.visualstudio.com/docs/copilot/customization/hooks)). Pick the "AAP Operator" agent
     in the chat view ([custom agents](https://code.visualstudio.com/docs/agents/reference/custom-agent-file-reference)).
   - Cursor: enable the three servers under Settings > MCP and check that the hooks show under Settings > Hooks.
     Project hooks run from the project root ([Cursor hooks](https://cursor.com/docs/agent/hooks)).
6. Verify. Ask the agent: "Check disk usage on web01." It should list templates labelled `mcp-agent`, launch
   `MON - Disk usage and largest directories` with `limit: web01` without a prompt, and report `aap_ops_result`. Then
   ask: "Restart nginx on web01." It should ask for a change number, and the editor should show a confirmation
   before `WF - MON - Restart service with validation` launches. Look at `.aap-mcp/audit.jsonl` to see both decisions.

## Why per-toolset endpoints and not discover

The MCP server also offers `/mcp/discover`, which exposes `discover` and `call_tool`. `call_tool` can call any tool
the token can reach, so neither editor's tool allowlist nor a per-tool approval would see the real operation. The
generated configs only use per-toolset URLs, and the gate denies `discover`, `call_tool`, and any server whose URL is
an AAP MCP endpoint but isn't one of the three gated names.

## Editor notes

- VS Code: `.mcp.json` limits each server to its allowlisted tools (`tools`), so other tools never reach the model.
  `.vscode/settings.json` turns off global auto-approve. A user setting can still override it, so the hook is what
  you rely on. When a hook times out, VS Code falls back to normal approvals; it does not allow the call
  ([VS Code hooks](https://code.visualstudio.com/docs/copilot/customization/hooks)). The gate accepts both the
  `preToolUse` (camelCase) and `PreToolUse` (snake_case) payloads, and matches AAP tool names with or without a
  server prefix ([GitHub Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference)).
- Cursor: `failClosed: true` means a crash, timeout or empty output blocks the call. The gate also exits 2 on
  internal errors, which Cursor treats as deny ([Cursor hooks](https://cursor.com/docs/agent/hooks)). Calls without
  `mcp_server_name` are denied.
- The shell rules are defense in depth. They catch obvious direct access, not an agent determined to evade them.
  The real controls are that the token can't start a changing template, the agent user can't approve, and the
  VS Code custom agent has no terminal tool.
- `python3` must be on the editor's `PATH`. On Windows, change the hook command to `py -3 tools/aap_mcp_gate.py ...`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Every launch denied: "Template IDs are not synced" | Run `python3 tools/aap_mcp_sync.py` |
| "ID n is not an agent-launchable catalog template" | The template isn't in the catalog, isn't launchable by the token, or was recreated; re-run the sync |
| Launch tools missing from the agent | `ALLOW_WRITE_OPERATIONS` is off on the MCP server; the sync prints a WARN |
| Cursor asks for every GitHub/other MCP call | Expected. Set `AAP_MCP_GATE_PASSTHROUGH=allow` if you want non-AAP servers to run without the gate's prompt |
| 401 from the servers | `AAP_MCP_TOKEN` not exported in the shell that started the editor, or the token expired |

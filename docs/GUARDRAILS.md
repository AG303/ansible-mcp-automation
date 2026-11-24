# Guardrails

The AAP 2.6 MCP server has to allow write operations before an agent can launch anything
([MCP configuration reference](https://docs.redhat.com/es/documentation/red_hat_ansible_automation_platform/2.6/extend-standalone_mcp_server_configuration_reference)).
So the controls live in AAP itself, and they come in layers.

## 1. RBAC (aap_config/vars/90_teams_rbac.yml)

| Team | Grants |
|---|---|
| `MCP Agents` | `execute` on the 60 non-changing job templates (read-only plus ServiceNow-write) and on the 23 `WF -` workflows; `read` on both inventories. No execute on any bare changing template. No execute on `SN - Change start` or `SN - Change close`, which only run as workflow nodes. |
| `AAP Ops Approvers` | `approval` on the 23 `WF -` workflows |

The agent's AAP user should belong to `MCP Agents` and nothing else. Keep that user out of org admin and out of
`AAP Ops Approvers`, so the agent can never approve its own request. `tests/validate_config.py` fails the build when
a changing template gets granted to agents or when a workflow has no approval node.

## 2. Change control plus approval on every change

Every template that changes systems is wrapped in `WF - <name>`:

```
SN - Change gate ──ok──> Approval (AAP Ops Approvers, timeout 4h) ──approved──> SN - Change start
     │ fail: stop                                                                    │
     ▼                                                                               ▼
  nothing runs                                [PATCH - Pre-patch readiness] ──> <changing template> ──> [PATCH - Post-patch validation]
                                                                                     │ success                 │ failure
                                                                                     ▼                         ▼
                                                                        SN - Change close (successful)   SN - Change close (unsuccessful)
                                                                                                         + SN - Incident from failed job
```

The gate (`sn_change` role defaults) requires all of the following:

- The change is approved, in the Scheduled or Implement state, and not on hold.
- The current time falls inside the planned window. Set `sn_change_gate_grace_minutes` to allow an early start.
- The change's correlation ID matches the workflow, for changes created by `SN - Create change request`.
- Every host in the workflow `limit` is a CI on the change. An empty limit fails this check.

Policy lives in role defaults, not in surveys. Workflows set `ask_variables_on_launch: false`, and only the six
workflow-node templates accept free-form vars, so an agent can't relax the gate at launch.

Approval timeout: `aap_ops_approval_timeout` (default 14400 s). An expired approval fails the workflow.

## 3. Blast radius

- Host templates use `ask_limit_on_launch`. The agent guide tells the agent to always set a narrow limit, and the gate
  enforces that the limit is covered by the change CIs.
- `PATCH - Apply updates` and `PATCH - Controlled reboot` run with `serial` (survey `serial_pct`, default 25%) and stop
  after the first batch that has a failure.
- `JOB - Launch template in batches` caps the batch size in its survey (1-1000 hosts). `JOB - Cancel stuck jobs` only touches jobs older than its survey threshold, which is at least 5 minutes and 240 by default.
- `SN - CMDB server CI sync` defaults to a dry run and is capped at `sn_cmdb_max_writes` (200).
  `SN - Problem candidates from incident trend` is capped at `sn_problem_trend_max_create` (10).
- Changing templates set `allow_simultaneous: false`.

## 4. Credentials

- Secrets reach AAP only through credentials built from `vault_*` variables (`aap_config/vault.example.yml`).
  Credential fields never go into surveys or job output. The AAP API role uses `no_log` on token handling, and the `servicenow.itsm` modules already mark the password `no_log`.
- The ServiceNow credential uses basic auth only, injected as `SN_HOST`, `SN_USERNAME` and `SN_PASSWORD`. Give the
  integration user just the roles it needs: `itil` for incident, change and problem, and CMDB write only if you use
  `sn_cmdb_sync` with `sn_dry_run=no`.
- The Windows account comes from its own custom credential type (`AAP Ops WinRM`), which injects
  `AAP_OPS_WIN_USERNAME` and `AAP_OPS_WIN_PASSWORD`. That way one template can carry both the Linux machine credential
  and the Windows account. WinRM certificate validation defaults to `validate`.
- The controller API credential should be a dedicated service-account token. Read-only audits need auditor rights;
  the changing JOB and PLAT templates need admin on the objects they touch.

## 5. Check mode

Every changing host template supports check mode (`job_type: check` or `-C`). Read-only commands are marked
`check_mode: false`, so a check run still reports real state. There are limits, though:

- When check mode would install a package that's missing (auditd, libpam-pwquality, ufw, firewalld), it can't check the
  steps that depend on that package. Those steps are tolerated and reported as `changed`.
- `MON - Restart service with validation` skips its post-restart wait, and `SEC - Audit logging configuration` skips
  rule verification.
- The OpenShift, AAP API and ServiceNow templates don't simulate their writes in check mode.

## 6. MCP server switch

`WF - PLAT - Enable or disable MCP server` patches `spec.mcp.disabled` on the `AnsibleAutomationPlatform` CR
([Red Hat](https://docs.redhat.com/it/documentation/red_hat_ansible_automation_platform/2.6/install-enable_the_mcp_server_on_an_existing_deployment)).
It's the kill switch for all agent access, and it goes through the same change gate and approval.

## 7. Editor agent gate

When the agent runs in VS Code (GitHub Copilot) or Cursor, a hook checks every AAP MCP call before it leaves the
editor ([EDITOR_AGENTS.md](EDITOR_AGENTS.md)). `tools/aap_mcp_gate.py` reads `agent/aap_mcp_policy.json`, which is
generated from `catalog.yml`:

- Read tools run. Read-only catalog templates launch without a prompt, but only with a narrow `limit` on host
  templates and only survey variables in `extra_vars`. Other launch fields are denied.
- ServiceNow-write templates and every `WF -` workflow need confirmation in the editor. A workflow also needs a
  `sn_change_number` that looks like `CHG` plus digits. After that, the ServiceNow gate and the `AAP Ops Approvers`
  approval in section 2 still apply.
- Bare changing templates, unknown IDs, cancel/relaunch, object create/update/delete, ad hoc commands, and every tool
  outside the allowlist are denied.
- The `/mcp/discover` endpoint is denied. Its `call_tool` tool can reach any tool the token can, which would bypass
  the per-tool allowlist. Connect the per-toolset endpoints only.
- Shell commands that use AAP token variables, call the AAP API or MCP URLs directly, run `awx`, or apply
  `configure_aap.yml` are denied.
- Errors fail closed, and every decision goes to `.aap-mcp/audit.jsonl`.

`tools/aap_mcp_sync.py` checks the token before the gate allows any launch. It fails if the agent user is a
superuser or an approver, if the token can start a changing or workflow-only job template, or if a workflow has no
approval node. Only after that does it write the template ID map that the gate uses.

The gate runs on the user's workstation, so it doesn't replace sections 1 and 2. Someone with the token can always
call AAP without the editor. RBAC, the change gate and the approval node are the hard boundary; the editor gate adds
an earlier human check and gives the agent clear errors.

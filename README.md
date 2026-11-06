# AAP 2.6 MCP Automation Collection

83 agent-launchable job templates and 23 approval-gated workflows for Red Hat Ansible
Automation Platform (AAP) 2.6, built so an AI agent connected through the AAP MCP server
can find the right template by name and description, fill the survey, launch it and read a
structured result.

| Domain | Code | Templates | Read-only | Changing (WF + approval) |
|---|---|---|---|---|
| Server Monitoring | MON | 13 | 11 | 2 |
| Job Management | JOB | 12 | 7 | 5 |
| Security Compliance (CIS or STIG) | SEC | 14 | 9 | 5 |
| Platform Configuration (OpenShift operator) | PLAT | 13 | 9 | 4 |
| Patch Management (vendor, Satellite, mirror, WSUS) | PATCH | 12 | 6 | 6 |
| ServiceNow incident, change and problem | SN | 19 | 18 (12 only write SN records) | 1 |
| Total | | 83 | 60 | 23 |

The full list with descriptions, surveys and playbook paths is in [docs/CATALOG.md](docs/CATALOG.md).

Platforms: RHEL 8/9/10, Ubuntu 22.04/24.04 and later, and Windows Server 2019/2022/2025. Every host
playbook works out the OS from facts. The AAP API, OpenShift and ServiceNow templates run on localhost
inside the execution environment.

## How it fits AAP 2.6 and the MCP server

AAP 2.6 ships the MCP server as a supported component. It exposes AAP to MCP clients through
toolsets, and the `job_management` toolset covers job templates, jobs, workflow jobs and projects
([Red Hat: About the Ansible MCP server](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/extend-con_about_ansible_mcp_server),
[MCP configuration reference](https://docs.redhat.com/es/documentation/red_hat_ansible_automation_platform/2.6/extend-standalone_mcp_server_configuration_reference)).
Launching templates is a POST, so the MCP server needs write operations turned on (`ALLOW_WRITE_OPERATIONS=true`).
That makes AAP RBAC the real guardrail. This project sets it up so the agent's account can execute only
read-only templates and the `WF -` workflows, and each workflow stops at a human approval node.

Agents authenticate to the MCP server with an AAP personal access token, or with OAuth if the client
supports it ([Red Hat: Authenticate with the MCP server](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/extend-authenticate_with_the_mcp_server)).
On OpenShift, set `spec.mcp.disabled: false` on the `AnsibleAutomationPlatform` CR
([Red Hat: Enable the MCP server on OCP](https://docs.redhat.com/it/documentation/red_hat_ansible_automation_platform/2.6/install-enable_the_mcp_server_on_an_existing_deployment)).
The `PLAT - Enable or disable MCP server` workflow does that for you.

Every job ends the same way. It publishes an `aap_ops_result` artifact through `set_stats`
(overall status, counts, and per-host status, summary and details), which the agent reads from the job
output. It also writes JSON and HTML reports to `aap_ops_report_dir` (default `/tmp/aap_ops_reports`).

## Layout

```
catalog.yml                     single source of truth (83 templates)
playbooks/<domain>/<id>.yml     generated thin playbooks (3 ServiceNow hybrids handwritten)
collections/ansible_collections/devops/aap_ops/   the collection: 18 roles
collections/requirements.yml    collections installed by project sync / EE build
aap_config/                     infra.aap_configuration config-as-code
  configure_aap.yml             applies vars/ to AAP
  vars/10..90_*.yml             generated labels, credential types, credentials, EE, project,
                                inventories, job templates, workflows, teams and RBAC
  vault.example.yml             secrets template
execution-environment/          ansible-builder v3 definition (AAP 2.6 ee-minimal base)
inventory/                      example inventory and group_vars (Windows WinRM, patch sources)
tools/gen_playbooks.py          catalog -> playbooks   (--check detects drift)
tools/gen_config.py             catalog -> aap_config/vars, docs/CATALOG.md, EE requirements
tests/                          AAP and ServiceNow mock APIs, test runners, config validator
docs/                           guides (below)
```

## Quick start

1. Build and push the execution environment:
   ```bash
   cd execution-environment
   ansible-builder build -t registry.example.com/aap-ops/ee:1.0.0
   podman push registry.example.com/aap-ops/ee:1.0.0
   ```
2. Push this directory to Git. The project points at it, and the playbooks live under `playbooks/`.
3. Make the secrets file: `cp aap_config/vault.example.yml aap_config/vault.yml`, fill it in, then run `ansible-vault encrypt aap_config/vault.yml`.
   The same file also sets `aap_ops_git_url`, `aap_ops_ee_image`, `aap_ops_organization`, `aap_ops_ocp_api_url` and `aap_ops_sn_instance_url`.
4. Apply the config as code:
   ```bash
   ansible-galaxy collection install -r collections/requirements.yml
   export AAP_HOSTNAME=https://aap.apps.example.com AAP_TOKEN=<admin token>
   ansible-playbook aap_config/configure_aap.yml -e @aap_config/vault.yml --ask-vault-pass
   ```
5. Add the managed hosts to the `AAP Ops - Managed Hosts` inventory, or attach your own dynamic source.
6. Create an AAP user for the agent, add it to the `MCP Agents` team, and issue it a token. Give that token to the MCP client.
   Put the humans who approve changes in `AAP Ops Approvers`.
7. Test it. Run `SN - ServiceNow connection test` and `MON - Host health check` with a small limit, then ask the agent
   "check disk usage on web servers" and watch it find and launch `MON - Disk usage and largest directories`.

## Documentation

- [docs/CATALOG.md](docs/CATALOG.md): every template, its kind, entry point, target and survey
- [docs/MCP_AGENT_GUIDE.md](docs/MCP_AGENT_GUIDE.md): how an agent should discover, launch and read results (you can paste it into a system prompt)
- [docs/GUARDRAILS.md](docs/GUARDRAILS.md): approval workflows, RBAC, check mode, and blast-radius controls
- [docs/SERVICENOW_INTEGRATION.md](docs/SERVICENOW_INTEGRATION.md): incident response, change control and problem management flows
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md): how to add a template, role contract, and result schema
- [docs/TESTING.md](docs/TESTING.md): mocks, test runners and what has (and has not) been tested
- [collections/ansible_collections/devops/aap_ops/README.md](collections/ansible_collections/devops/aap_ops/README.md): role reference

## Versions

AAP 2.6 execution environments default to ansible-core 2.16 on Python 3.12
([Red Hat AAP life cycle](https://access.redhat.com/support/policy/updates/ansible-automation-platform),
[AAP 2.6 release notes, Feb 2026](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/whats_new-aap_26_2026_02_25)).
Every playbook was tested on ansible-core 2.16.20 and on 2.21.2. `community.general` is pinned to `<12.0.0`
because 12.x and later dropped ansible-core 2.16. Config as code uses
[infra.aap_configuration](https://github.com/redhat-cop/infra.aap_configuration) 3.x.

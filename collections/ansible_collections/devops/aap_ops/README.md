# devops.aap_ops

These are the roles behind the 83 job templates of the AAP 2.6 MCP Automation Collection. Each role exposes task
files (`tasks_from`), and each task file backs exactly one template. They all share one result contract, so every job
publishes the same `aap_ops_result` artifact (see the project's `docs/CONVENTIONS.md`).

Requirements: ansible-core 2.16 or later (tested on 2.16.20 and 2.21.2) and Python 3.12 in the execution environment.
The dependencies are listed in `galaxy.yml`: ansible.posix, ansible.utils, ansible.windows, community.general
(below 12.0.0 so ansible-core 2.16 keeps working), community.windows, kubernetes.core, servicenow.itsm.

## Roles

| Role | Target | Task files |
|---|---|---|
| `common` | managed hosts | `main` (facts, OS detection, unreachable handling, target tracking) |
| `report` | localhost | `main` (aggregate, `set_stats` aap_ops_result, JSON/HTML report, fail gate) |
| `host_monitor` | RHEL, Ubuntu, Windows | `health_check`, `disk_usage`, `service_status`, `port_check`, `log_errors`, `top_processes`, `cert_expiry`, `time_sync`, `network_diag`, `system_inventory`, `fleet_report`, `service_restart`, `disk_cleanup` |
| `compliance_baseline` | RHEL, Ubuntu, Windows | `scan`, `remediate` (CIS Level 1 or DISA STIG subset, chosen with `compliance_profile`) |
| `host_hardening` | RHEL, Ubuntu, Windows | `ssh_hardening`, `password_policy`, `firewall_baseline`, `audit_logging` |
| `host_security_audit` | RHEL, Ubuntu, Windows | `user_audit`, `mac_status`, `file_permissions_audit`, `open_ports_audit` |
| `patch_manage` | RHEL, Ubuntu, Windows | `available`, `precheck`, `apply`, `postcheck`, `reboot_required`, `reboot`, `cve_check`, `package`, `compliance_report`, `kernel_cleanup`, `package_hold` |
| `patch_source` | RHEL, Ubuntu, Windows | `configure` (vendor CDN, RHSM, Satellite, internal mirror, WSUS) |
| `aap_api` | AAP gateway/controller API | connection (`main`), `get`, `request`, `wait_job`, `job_evidence` |
| `aap_jobs` | AAP API | `failed_summary`, `long_running`, `cancel_stuck`, `relaunch_failed`, `schedule_audit`, `schedule_toggle`, `template_audit`, `capacity_queue`, `host_failure_trend`, `launch_template`, `cleanup_history`, `workflow_approvals` |
| `aap_security_audit` | AAP API | `credential`, `rbac`, `activity`, `token` |
| `aap_platform_config` | AAP API | `instance_groups`, `ee_audit`, `ee_register`, `settings_export`, `config_drift`, `config_apply`, `subscription_usage` |
| `aap_operator` | OpenShift (kubernetes.core) | `health_status`, `mcp_status`, `mcp_configure`, `operator_status`, `backup`, `backup_audit` |
| `sn_api` | ServiceNow Table API | connection (`main`), `connection_test`, `query`, `lookup`, `work_note`, `incident_upsert` |
| `sn_incident` | ServiceNow plus hosts | `create`, `from_job`, `update`, `list`, `resolve_targets`, `diagnose_host`, `remediate_host`, `post_diagnostics`, `post_remediation` |
| `sn_change` | ServiceNow | `create`, `conflict_check`, `schedule`, `gate`, `start`, `close` |
| `sn_problem` | ServiceNow | `create`, `from_trend`, `rca`, `update`, `list` |
| `sn_cmdb` | ServiceNow plus hosts | `host_facts`, `sync` |

Files whose names start with `_` are internal helpers. Each role documents its inputs in `meta/argument_specs.yml` and
its defaults in `defaults/main.yml`.

## Connections

- AAP API: `AAP_HOSTNAME` (or `CONTROLLER_HOST`) and `AAP_TOKEN` (or `CONTROLLER_OAUTH_TOKEN`, or `AAP_USERNAME`/`AAP_PASSWORD`), injected by the
  "Red Hat Ansible Automation Platform" credential. Gateway paths are `/api/controller/v2/`.
- OpenShift: `K8S_AUTH_HOST` and `K8S_AUTH_API_KEY` from the OpenShift or Kubernetes bearer-token credential. The namespace
  and CR name come from `aap_namespace` and `aap_cr_name` (default `aap`/`aap`).
- ServiceNow: `SN_HOST`, `SN_USERNAME`, `SN_PASSWORD` (basic auth), used through `servicenow.itsm.api` and `api_info`.
- Windows: WinRM, with the account taken from `AAP_OPS_WIN_USERNAME` and `AAP_OPS_WIN_PASSWORD` (see
  `inventory/group_vars/windows.yml`).

Include connection roles with `public: true` so their facts and defaults reach the task file that follows.

## Example

```yaml
- name: Disk usage on web tier
  hosts: web
  gather_facts: false
  tasks:
    - ansible.builtin.include_role: {name: devops.aap_ops.common}
    - ansible.builtin.include_role: {name: devops.aap_ops.host_monitor, tasks_from: disk_usage}
      vars: {disk_warn_pct: 80}

- name: Report
  hosts: localhost
  gather_facts: false
  vars:
    aap_ops_report_title: Disk usage
    aap_ops_report_hosts: "{{ hostvars['localhost']._aap_ops_targets | default([]) }}"
  tasks:
    - ansible.builtin.include_role: {name: devops.aap_ops.report}
```

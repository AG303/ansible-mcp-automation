# ServiceNow Integration

There are 19 `SN -` templates covering incident response, change control, problem management and the CMDB.
They use the Red Hat certified [servicenow.itsm collection](https://github.com/ansible-collections/servicenow.itsm)
through its generic `api` and `api_info` Table API modules, with basic auth only.

The problem flows talk to the Table API directly (`problem`, `problem_task`, `incident`), so you don't need the optional
"Ansible API for ServiceNow ITSM" store app. The `servicenow.itsm.problem` module does need that app for state transitions
([Red Hat overview](https://www.redhat.com/en/resources/servicenow-itsm-overview)).

## Setup

1. In ServiceNow, create an integration user (for example `aap.integration`) with basic auth enabled. It needs the
   `itil` role for incident, change and problem. Add CMDB write rights only if you will run `SN - CMDB server CI sync`
   with `sn_dry_run=no`.
2. In `aap_config/vault.yml`, set `aap_ops_sn_instance_url`, `vault_sn_username` and `vault_sn_password`. This creates
   the `AAP Ops - ServiceNow` credential, of type `ServiceNow ITSM (basic auth)`, which injects `SN_HOST`, `SN_USERNAME`
   and `SN_PASSWORD`.
3. Optional: set `SN_ASSIGNMENT_GROUP` in the credential type's injectors, or `sn_default_assignment_group`, to choose
   the default group for records the automation creates.
4. Run `SN - ServiceNow connection test`. It checks reachability, the build version, and read access to `incident`,
   `change_request`, `problem` and `cmdb_ci`.

Choice values follow the out-of-box ServiceNow configuration (`sn_api` defaults: `sn_incident_state_map`,
`sn_change_state_map`, `sn_problem_state_map`, `sn_impact_map`, `sn_risk_map`). If your instance has customized
choice lists, override those maps.

## Correlation and de-duplication

Records the automation creates get a `correlation_id` of `aap_ops:<kind>:<key>` and a `correlation_display` of
"Ansible Automation Platform":

- `aap_ops:<template>:<limit or all>` (from failed jobs) and `aap_ops:manual:<slug>` (from `SN - Create incident`, unless you
  pass `sn_correlation_id`): when an open incident already has the same correlation ID, the automation adds a work note
  to it instead of opening a duplicate. Incident targeting also reads the host part of the correlation ID.
- `aap_ops:change:<workflow name>`: the change gate checks that a change made for one workflow is not used to
  authorize a different one.
- `SN - Problem candidates from incident trend` groups incidents by CI and correlation ID.

Work notes link to the AAP job or workflow job output (`<AAP_HOSTNAME>/execution/jobs/...`).

## Incident response

| Step | Template | Who launches it |
|---|---|---|
| Detect | Any template fails inside a `WF -` workflow | Automatic: the failure path runs `SN - Incident from failed job` |
| Detect | A job fails outside a workflow | The agent runs `SN - Incident from failed job` with `sn_source_job_id` |
| Triage | `SN - Open incidents report` (filter by group, priority, AAP-only) | Agent |
| Diagnose | `SN - Incident diagnostics`: health, disk, services and recent errors on the incident CI, posted as a work note | Agent (read-only on hosts) |
| Remediate | `WF - SN - Incident remediation`: restart_service, disk_cleanup or clear_temp, then a health re-check, then resolve | Agent launches; CR gate plus AAP approval |
| Update or close | `SN - Update or resolve incident` | Agent |

`SN - Incident from failed job` reads the failed job, or for a workflow its failed nodes. It collects the failed hosts
and the failing task and message, puts that evidence in the incident description, and sets the CI when a single host
failed.

Diagnostics and remediation work out their target hosts from the incident's `cmdb_ci` (short name matched against
the inventory) or from the launch `limit`.

## Change control

The answer chosen for this project was "CR gate + AAP approval". A changing workflow needs both an approved
ServiceNow change and an AAP approval.

```
Agent/user: SN - Create change request  ->  CAB approves in ServiceNow  ->  WF - <template> (sn_change_number=CHG...)
              gate -> AAP approval -> SN - Change start (Implement + work note) -> template -> SN - Change close (Review)
```

- `SN - Create change request`: normal, standard or emergency. It takes the planned window (UTC,
  `YYYY-MM-DD HH:MM:SS`) and the affected hosts, which become CIs through `task_ci` when they exist in the CMDB. It also
  fills in implementation, backout and test plans that reference the AAP workflow.
- `SN - Change conflict check`: overlapping changes on the same CIs, plus blackout schedules.
- `SN - Upcoming and active changes`: scheduled and implementing changes, approved changes with no AAP run, and expired windows.
- `SN - Change gate`, `SN - Change start` and `SN - Change close` run only as workflow nodes. The close notes include
  per-host results from the main job (`sn_close_code`: successful, successful_issues or unsuccessful).

Gate policy settings (role `sn_change` defaults): `sn_change_gate_states`, `sn_change_gate_require_ci_coverage`,
`sn_change_gate_require_template_match`, `sn_change_gate_grace_minutes`.

## Problem management

| Template | Purpose |
|---|---|
| `SN - Problem candidates from incident trend` | Groups the last N days of incidents by CI or correlation ID. Groups at or above `min_incidents` become candidates. With `create_problems=yes`, it opens problems (max 10 per run) and links the incidents. |
| `SN - Create problem` | Opens a problem and links the listed incidents |
| `SN - Problem root cause evidence` | Collects linked incidents, AAP job failures on the same hosts and templates, and failing tasks, then posts them as a root cause analysis `problem_task` |
| `SN - Update or resolve problem` | Records cause and fix notes, moves the state, or resolves it (fix_applied, risk_accepted, duplicate, canceled) |
| `SN - Open problems report` | State, age, linked incident count and known-error flag |

## CMDB

`SN - CMDB server CI sync` compares host facts (OS, version, serial, IP, CPU, RAM, FQDN) with `cmdb_ci_linux_server`
and `cmdb_ci_win_server`, and creates or updates them. It defaults to a dry run and is capped at 200 writes per run.
Accurate CIs matter because the change gate's CI coverage check and incident targeting both depend on them.

# Testing

## Static checks (run in CI)

```bash
python3 tools/gen_playbooks.py --check      # playbooks match catalog.yml
python3 tools/gen_config.py --check         # aap_config/vars, CATALOG.md, EE requirements match
python3 tests/validate_config.py            # referential integrity and guardrail rules
for p in playbooks/*/*.yml; do ansible-playbook --syntax-check "$p"; done
ansible-lint                                # production profile, 0 failures expected
yamllint -c .yamllint .
```

`validate_config.py` checks the following:

- Every job template points at a playbook, inventory, credential, EE and project that exist.
- Every survey default is valid for its type and choices.
- Every changing template has a `WF -` workflow with exactly one approval node.
- Workflow nodes reference real templates, the first node is the change gate, and `sn_change_number` is the first required survey question.
- Node `extra_data` only targets templates that accept launch variables.
- The `MCP Agents` team can't execute a changing job template directly.

## Functional tests

### Host playbooks: `tests/run_host_tests.py`

```bash
export ANSIBLE_COLLECTIONS_PATH=~/.ansible/collections:$PWD/collections
python3 tests/run_host_tests.py                 # all host templates against localhost
python3 tests/run_host_tests.py mon_disk_usage  # just one
AAP_OPS_TEST_NO_SYSTEMD=1 python3 tests/run_host_tests.py   # containers without a systemd D-Bus
```

Read-only templates run for real. Changing templates run in check mode, so the test host isn't modified. A template
passes when it publishes a summary and nothing failed outside the report's own fail-on-status gate. So `rc=2` together
with `fail=1` means a real finding on the test host, such as no time sync or AppArmor being off, and that still counts
as a pass.

### AAP API and ServiceNow: `tests/run_api_tests.py`

```bash
python3 tests/mock_aap.py 8099 &            # AAP 2.6 gateway/controller API mock
python3 tests/mock_servicenow.py 8098 &     # ServiceNow Table API mock (seeded INC/CHG/PRB/CMDB)
python3 tests/run_api_tests.py
```

The runner sets `AAP_HOSTNAME`, `AAP_TOKEN`, `SN_HOST`, `SN_USERNAME` and `SN_PASSWORD` to the mocks, then runs every
`aap_api` and `servicenow` template in lifecycle order: create incident, update, diagnose, remediate, change create,
gate, start, close, problem create, RCA, resolve. It also runs the two hybrid playbooks (diagnostics, remediation)
against localhost. The ServiceNow mock supports encoded queries (`=`, `!=`, `IN`, `NOT IN`, `STARTSWITH`, `LIKE`,
`ISEMPTY`, `>`, `>=`, `<`, `<=`, `^OR`; `ORDERBY` is accepted and ignored), `sysparm_display_value`, `X-Total-Count`, and POST/PATCH. Pass/fail is based on each job's
summary and on the absence of rescued errors. To check record state transitions and work notes, query the mock
afterwards (for example `curl -u x:y 'http://127.0.0.1:8098/api/now/table/change_request?sysparm_query=number=CHG0001001'`).
For this release, those were checked by hand.

## Results for this release (2026-11-05)

| Check | ansible-core 2.16.20 (AAP 2.6 default) | ansible-core 2.21.2 |
|---|---|---|
| Syntax check, 83 playbooks | pass | pass |
| ansible-lint (production profile) | n/a | 0 failures (11 jinja[spacing] warnings) |
| validate_config.py | pass | pass |
| Host templates on Ubuntu (34; changing ones in check mode) | 34 pass | 34 pass |
| AAP API templates against the mock (22) | 22 pass | 22 pass |
| ServiceNow templates against the mock (19) | 19 pass | 19 pass |

These have not been tested against a live system, and you should run them in a lab before production:

- RHEL and Windows code paths. The host tests ran on Ubuntu only. The RHEL tasks (dnf, firewalld, SELinux, authselect,
  RHSM/Satellite) and the Windows tasks (WinRM, Windows Update, Defender, secedit) have been syntax- and lint-checked
  but not run.
- The six OpenShift templates (`plat_health_status`, `plat_mcp_status`, `plat_mcp_configure`, `plat_operator_status`,
  `plat_backup`, `plat_backup_audit`). The only check was that they fail gracefully with a clear message when there is
  no cluster. Check the CR field names against your operator version: `spec.mcp.disabled`, and the
  `AnsibleAutomationPlatformBackup` CR ([Red Hat backup docs](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/administer-assembly_aap_backup)).
- `PLAT - Apply configuration as code` (needs a real AAP to dispatch `infra.aap_configuration` against).
- `MON - Restart service with validation` (the test container has no systemd D-Bus).
- A real ServiceNow instance. The mock follows Table API semantics, but customized choice lists, business rules, and the
  change approval engine can behave differently. Run `SN - ServiceNow connection test` first, then the change lifecycle
  on a sub-production instance.
- The workflow graphs themselves (approval nodes, failure paths). They are generated and validated structurally, and
  have to be applied to an AAP 2.6 instance to run end to end.

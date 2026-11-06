# Conventions

## Single source of truth

`catalog.yml` defines every template: id, name, agent-facing description, `changes`, `target`, optional
`writes: servicenow`, OS list and survey. The header comments in that file document every field. Two generators read it:

| Generator | Output | Drift check |
|---|---|---|
| `tools/gen_playbooks.py` | `playbooks/<domain>/<id>.yml` (skips the 3 handwritten ServiceNow hybrids) | `--check` |
| `tools/gen_config.py` | `aap_config/vars/*.yml`, `docs/CATALOG.md`, `execution-environment/requirements.yml` | `--check` |

Don't hand-edit the generated files. Edit the catalog or the role, then regenerate. Run both `--check` commands in CI.

## Adding a template

1. Add a catalog entry. If it changes systems, start the description with `CHANGES SYSTEMS.` and end it with
   `Use the WF workflow (approval required).` If it's read-only, start with `Read-only.` Say what it returns.
2. Add a task file to an existing role, or create a role, plus an `argument_specs` entry. If the role name isn't
   obvious from the id, map it in `route()` in `gen_playbooks.py`.
3. Set `aap_ops_host_result` (host targets) or `aap_ops_report_rows` (API targets). See the contract below.
4. Regenerate, then run `ansible-playbook --syntax-check`, `ansible-lint`, `python3 tests/validate_config.py`, and the
   relevant test runner.

## Generated playbook shape

```
play 1  hosts: "{{ aap_ops_target | default('all') }}"      (target=hosts)
        or localhost with the aap_api / openshift / sn_api connection role (public: true)
  - include_role devops.aap_ops.common        facts, OS detection, unreachable handling, target tracking
  - include_role <role> tasks_from <task>     the work
play 2  hosts: localhost
  - include_role devops.aap_ops.report        aggregate, set_stats aap_ops_result, JSON/HTML, fail gate
```

`patch_apply` and `patch_reboot` add `serial: "{{ aap_ops_serial }}"` and stop after a batch that has a failure.

## Result contract

Host roles set one fact for each host:

```yaml
aap_ops_host_result:
  status: ok | warn | fail | changed | skipped
  summary: "one line a human or agent can repeat"
  details: {}        # structured, kept small; truncated to aap_ops_stats_detail_chars in the artifact
```

API, OpenShift and ServiceNow roles append rows with the same keys plus `host` (the object name) to `aap_ops_report_rows`.
Error handling: wrap work in `block`/`rescue`, and in `rescue` set `status: fail` with
`summary: "<what> failed at \"<task name>\": <message>"`. A host should never just drop out of the play.

## Variable naming

- `aap_ops_*`: framework variables (target, report, serial, connection).
- `<role>_*`: role tunables in `defaults/main.yml`.
- Survey variables are short and unprefixed (`service_name`, `compliance_profile`), and roles map them to role variables.
  Surveys only cover what an operator should decide per run. Policy (gates, thresholds that guard safety) stays in
  defaults or inventory.
- `sn_*`: ServiceNow role inputs. `_`-prefixed names are internal registers.

## Platform handling

`common` sets `aap_ops_os` to `rhel`, `ubuntu`, `windows` or `unsupported`. Tasks branch on it with `when:` and
`ansible.windows.*` modules for Windows. When a task file doesn't support an OS, it reports `skipped`. Read-only
`command`/`shell` tasks set `changed_when: false` and `check_mode: false`.

## Lint

`.ansible-lint` uses the production profile. The skips are listed in the file, each with a reason. `.yamllint`
allows long lines because some survey and description strings are long. The current baseline is 0 failures; the remaining jinja[spacing] warnings are left in place on purpose.

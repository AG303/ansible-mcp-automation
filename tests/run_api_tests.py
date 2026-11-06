#!/usr/bin/env python3
"""Run every aap_api and servicenow playbook against the local mocks with survey defaults.

Usage:
  python3 tests/mock_aap.py 8099 &  python3 tests/mock_servicenow.py 8098 &
  python3 tests/run_api_tests.py [id ...]
Prints one line per template: rc, and the report summary or the first failure.
ServiceNow templates run in lifecycle order (create -> gate -> start -> close ...).
"""
import json
from datetime import datetime, timedelta, timezone
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = {
    "job_relaunch_failed": {"job_id": 1},
    "job_schedule_toggle": {"name_filter": "Demo"},
    "job_launch_template": {"launch_template_name": "Demo Job Template", "launch_limit": "a,b,c", "batch_size": 2},
    "plat_ee_register": {"ee_name": "Test EE", "ee_image": "registry.example.com/test:1"},
}
_S, _E = (datetime.now(timezone.utc) + timedelta(hours=h) for h in (0, 2))
WINDOW = {"sn_planned_start": _S.strftime("%Y-%m-%d %H:%M:%S"), "sn_planned_end": _E.strftime("%Y-%m-%d %H:%M:%S")}
OVERRIDES.update({
    "sn_incident_create": {"sn_short_description": "Test incident from AAP", "sn_cmdb_ci": "localhost"},
    "sn_incident_from_job": {"sn_source_job_id": 1},
    "sn_incident_update": {"sn_incident_number": "INC0001001", "sn_incident_state": "in_progress", "sn_work_note": "Investigating"},
    "sn_incident_diagnostics": {"sn_incident_number": "INC0001001", "service_names": "cron"},
    "sn_incident_remediate": {"sn_incident_number": "INC0001002", "remediation_action": "clear_temp", "resolve_on_success": "yes"},
    "sn_change_create": dict(WINDOW, sn_template_name="PATCH - Apply updates", sn_short_description="Monthly patching",
                             sn_affected_hosts="localhost,web01.example.com"),
    "sn_change_conflict_check": dict(WINDOW, sn_affected_hosts="localhost"),
    "sn_change_gate": {"sn_change_number": "CHG0001001"},
    "sn_change_start": {"sn_change_number": "CHG0001001"},
    "sn_change_close": {"sn_change_number": "CHG0001001", "sn_close_code": "successful"},
    "sn_problem_create": {"sn_short_description": "Recurring disk alerts", "sn_incident_numbers": "INC0001003,INC0001004"},
    "sn_problem_from_trend": {"create_problems": "yes", "min_incidents": 2},
    "sn_problem_rca": {"sn_problem_number": "PRB0001001"},
    "sn_problem_update": {"sn_problem_number": "PRB0001001", "sn_problem_state": "root_cause_analysis", "sn_cause_notes": "Disk fills from logs"},
    "sn_cmdb_sync": {"sn_dry_run": "no"},
})
ORDER = ["sn_connection_test", "sn_incident_create", "sn_incident_from_job", "sn_incident_update", "sn_incident_list",
         "sn_incident_diagnostics", "sn_incident_remediate", "sn_change_create", "sn_change_conflict_check",
         "sn_change_schedule", "sn_change_gate", "sn_change_start", "sn_change_close", "sn_problem_create",
         "sn_problem_from_trend", "sn_problem_rca", "sn_problem_update", "sn_problem_list", "sn_cmdb_sync"]
HOST_SN = {"sn_incident_diagnostics", "sn_incident_remediate", "sn_cmdb_sync"}
SKIP = {"plat_config_apply"}  # dispatches infra.aap_configuration against the API; covered by aap_config tests

cat = yaml.safe_load((ROOT / "catalog.yml").read_text())
only = set(sys.argv[1:])
env = dict(os.environ, SN_HOST="http://127.0.0.1:8098", SN_USERNAME="aap.integration", SN_PASSWORD="test",
           AAP_HOSTNAME="http://127.0.0.1:8099", AAP_TOKEN="test", AAP_VALIDATE_CERTS="false",
           ANSIBLE_COLLECTIONS_PATH=os.environ.get("ANSIBLE_COLLECTIONS_PATH", f"/tmp/acol:{ROOT}/collections"),
           ANSIBLE_NOCOLOR="1")
bad = 0
tpls = [t for t in cat["templates"] if t["target"] == "aap_api"]
by_id = {t["id"]: t for t in cat["templates"]}
tpls += [by_id[i] for i in ORDER]
for t in tpls:
    if t["id"] in SKIP or (only and t["id"] not in only):
        continue
    ev = {s["var"]: s.get("default") for s in t.get("survey", [])}
    ev.update(OVERRIDES.get(t["id"], {}))
    pb = ROOT / "playbooks" / t["domain"] / f"{t['id']}.yml"
    cmd = ["ansible-playbook", str(pb), "-e", json.dumps(ev)]
    if t["id"] in HOST_SN:
        cmd += ["-i", "localhost,", "-c", "local", "-e", "aap_ops_target=localhost"]
    r = subprocess.run(cmd, cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    m = re.search(r'Print summary.*?"msg": \[\s*"([^"]+)"', out, re.S)
    fail = re.findall(r"(?:fatal|failed): \[.*?\]: FAILED! => (.{0,400})", out)
    rescued = re.findall(r'failed at \\"([^\\]+)\\": ([^"]{0,300})', out)
    status = "OK " if m and not rescued else "BAD"
    if status == "BAD":
        bad += 1
    print(f"{status} {t['id']:28} rc={r.returncode} {m.group(1) if m else ''}")
    for x in (rescued[:2] if rescued else ([] if m else fail[:1])):
        print("     ", x)
print(f"{bad} problem template(s)")

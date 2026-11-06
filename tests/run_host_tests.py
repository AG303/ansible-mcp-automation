#!/usr/bin/env python3
"""Run every target=hosts playbook (except the ServiceNow hybrids) against localhost.

Read-only templates run normally. Changing templates run in check mode (-C) so the
test host is not modified. Survey defaults are used, plus OVERRIDES for required values.

Usage:
  python3 tests/run_host_tests.py [id ...]
Prints one line per template with rc and the report summary. A template is BAD when it
produced no report summary or a task failed outside the report's own fail-on-status gate.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = {
    "mon_service_status": {"service_names": "cron"},
    "mon_service_restart": {"service_name": "cron"},
    "mon_port_check": {"endpoints": "127.0.0.1:22"},
    "patch_cve_check": {"package_name": "openssl"},
    "patch_package": {"package_names": "tree"},
    "patch_package_hold": {"package_names": "bash"},
}
# Set AAP_OPS_TEST_NO_SYSTEMD=1 on hosts/containers without a systemd D-Bus (service module cannot run)
NEEDS_SYSTEMD = {"mon_service_restart"}
SKIP = {"sn_incident_diagnostics", "sn_incident_remediate", "sn_cmdb_sync"}  # covered by run_api_tests.py

cat = yaml.safe_load((ROOT / "catalog.yml").read_text())
only = set(sys.argv[1:])
env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=os.environ.get("ANSIBLE_COLLECTIONS_PATH", f"/tmp/acol:{ROOT}/collections"),
           ANSIBLE_NOCOLOR="1")
bad = 0
for t in cat["templates"]:
    if t["target"] != "hosts" or t["id"] in SKIP or (only and t["id"] not in only):
        continue
    if t["id"] in NEEDS_SYSTEMD and os.environ.get("AAP_OPS_TEST_NO_SYSTEMD"):
        print(f"SKIP       {t['id']:28} needs a systemd D-Bus on the test host")
        continue
    ev = {s["var"]: s.get("default") for s in t.get("survey", [])}
    ev.update(OVERRIDES.get(t["id"], {}))
    pb = ROOT / "playbooks" / t["domain"] / f"{t['id']}.yml"
    cmd = ["ansible-playbook", str(pb), "-i", "localhost,", "-c", "local",
           "-e", "aap_ops_target=localhost", "-e", json.dumps(ev)]
    if t["changes"]:
        cmd.append("-C")
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    out = r.stdout + r.stderr
    m = re.search(r'Print summary.*?"msg": \[\s*"([^"]+)"', out, re.S)
    # A non-zero rc is expected when the report gate fails the job because a host is in a
    # failing status (real findings). Anything else that failed un-rescued is a defect.
    tasks = re.split(r"\nTASK \[", out)
    fails = [t.split("\n", 1)[0] for t in tasks if "fatal: [" in t and "failing status" not in t.split("\n", 1)[0]]
    fails += re.findall(r'(\S+: [^"]*? failed at \\"[^"]*)', out)  # rescued task errors
    gate = not fails
    status = "OK " if m and (r.returncode == 0 or gate) else "BAD"
    if status == "BAD":
        bad += 1
    mode = "check" if t["changes"] else "run  "
    print(f"{status} {mode} {t['id']:28} rc={r.returncode} {m.group(1) if m else ''}")
    if status == "BAD":
        for x in fails[:2] or [out[-600:]]:
            print("     ", x)
print(f"{bad} problem template(s)")

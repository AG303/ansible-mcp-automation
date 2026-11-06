#!/usr/bin/env python3
"""Generate playbooks/<domain>/<id>.yml from catalog.yml.

Every generated playbook is a thin wrapper: bootstrap (common / API connection)
-> one role task file -> devops.aap_ops.report. Hybrid ServiceNow playbooks that
span localhost and managed hosts are listed in HANDWRITTEN and are not touched.

Usage: python3 tools/gen_playbooks.py [--check]
"""
import sys
import textwrap
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
NS = "devops.aap_ops"
HANDWRITTEN = {"sn_incident_diagnostics", "sn_incident_remediate", "sn_cmdb_sync"}
SERIAL = {"patch_apply", "patch_reboot"}

# id -> (role, tasks_from)
def route(tid):
    special = {
        "sec_compliance_scan": ("compliance_baseline", "scan"),
        "sec_compliance_remediate": ("compliance_baseline", "remediate"),
        "sec_ssh_hardening": ("host_hardening", "ssh_hardening"),
        "sec_password_policy": ("host_hardening", "password_policy"),
        "sec_firewall_baseline": ("host_hardening", "firewall_baseline"),
        "sec_audit_logging": ("host_hardening", "audit_logging"),
        "sec_aap_credential_audit": ("aap_security_audit", "credential"),
        "sec_aap_rbac_audit": ("aap_security_audit", "rbac"),
        "sec_aap_activity_audit": ("aap_security_audit", "activity"),
        "sec_aap_token_audit": ("aap_security_audit", "token"),
        "patch_source_configure": ("patch_source", "configure"),
        "sn_connection_test": ("sn_api", "connection_test"),
    }
    if tid in special:
        return special[tid]
    if tid.startswith("mon_"):
        return "host_monitor", tid[4:]
    if tid.startswith("sec_"):
        return "host_security_audit", tid[4:]
    if tid.startswith("patch_"):
        return "patch_manage", tid[6:]
    if tid.startswith("job_"):
        return "aap_jobs", tid[4:]
    if tid.startswith("plat_"):
        t = tid[5:]
        if t in {"health_status", "mcp_status", "mcp_configure", "operator_status", "backup", "backup_audit"}:
            return "aap_operator", t
        return "aap_platform_config", t
    for p in ("sn_incident_", "sn_change_", "sn_problem_"):
        if tid.startswith(p):
            return p[:-1], tid[len(p):]
    raise SystemExit(f"no route for {tid}")


# ServiceNow templates that also call the AAP API; value = only inside a workflow
SN_NEEDS_AAP = {"sn_incident_from_job": False, "sn_problem_rca": False,
                "sn_change_gate": True, "sn_change_close": True}


def header(t, domain_title):
    lines = [
        "---",
        f"# {t['name']}",
        f"# Domain: {domain_title}. Generated from catalog.yml by tools/gen_playbooks.py - edit the catalog/role, not this file.",
    ]
    lines += ["# " + l for l in textwrap.wrap(t["description"], 110)]
    if t.get("survey"):
        lines += textwrap.wrap("Survey variables: " + ", ".join(s["var"] for s in t["survey"]), 110, initial_indent="# ", subsequent_indent="#   ")
    return "\n".join(lines) + "\n"


def host_playbook(t, domain_title):
    role, task = route(t["id"])
    os_list = t.get("os", ["rhel", "ubuntu", "windows"])
    pvars = {"aap_ops_report_title": t["name"]}
    if sorted(os_list) != ["rhel", "ubuntu", "windows"]:
        pvars["aap_ops_supported_os"] = os_list
    play = {
        "name": t["name"],
        "hosts": "{{ aap_ops_target | default('all') }}",
        "gather_facts": False,
        "become": True,
    }
    if t["id"] in SERIAL:
        # Rolling batches; a failed batch stops later batches (see "Halt" task).
        play["serial"] = "{{ aap_ops_serial }}"
        pvars["aap_ops_serial"] = "{{ serial_pct | default(25) }}%"
    play["vars"] = pvars
    tasks = [{"name": "Bootstrap (facts, OS detection, target tracking)",
              "ansible.builtin.include_role": {"name": f"{NS}.common"}}]
    if t["id"] in SERIAL:
        tasks.append({
            "name": "Halt remaining batches after a failed batch",
            "when": "hostvars['localhost']._aap_ops_batch_failed | default(false) | bool",
            "block": [
                {"name": "Mark host skipped",
                 "ansible.builtin.set_fact": {"aap_ops_host_result": {
                     "status": "skipped", "summary": "Not attempted - an earlier rolling batch failed", "details": {}}}},
                {"name": "Stop processing this host", "ansible.builtin.meta": "end_host"},
            ]})
    tasks.append({"name": f"Run {role}/{task}",
                  "ansible.builtin.include_role": {"name": f"{NS}.{role}", "tasks_from": task}})
    if t["id"] in SERIAL:
        tasks.append({
            "name": "Record batch failure for the next batch",
            "run_once": True, "delegate_to": "localhost", "delegate_facts": True,
            "ansible.builtin.set_fact": {"_aap_ops_batch_failed": (
                "{{ hostvars['localhost']._aap_ops_batch_failed | default(false) | bool or 'fail' in _batch_status }}")},
            "vars": {"_batch_status": (
                "{{ ansible_play_batch | map('extract', hostvars) | map(attribute='aap_ops_host_result', default={}) "
                "| map(attribute='status', default='fail') | list }}")}})
    play["tasks"] = tasks
    report = {
        "name": f"Report - {t['name']}",
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {"aap_ops_report_title": t["name"],
                 "aap_ops_report_hosts": "{{ hostvars['localhost']._aap_ops_targets | default([]) }}"},
        "tasks": [{"name": "Publish aap_ops_result and reports",
                   "ansible.builtin.include_role": {"name": f"{NS}.report"}}],
    }
    return [play, report]


def api_playbook(t, domain_title):
    role, task = route(t["id"])
    tasks = []
    if t["target"] == "aap_api":
        tasks.append({"name": "AAP API connection", "ansible.builtin.include_role": {"name": f"{NS}.aap_api", "public": True}})
    if t["target"] == "servicenow":
        # public: the sn_api defaults (state maps, correlation prefix, limits) are shared by every sn_* role
        tasks.append({"name": "ServiceNow connection", "ansible.builtin.include_role": {"name": f"{NS}.sn_api", "public": True}})
        if t["id"] in SN_NEEDS_AAP:
            step = {"name": "AAP API connection (job evidence / workflow context)",
                    "ansible.builtin.include_role": {"name": f"{NS}.aap_api", "public": True}}
            if SN_NEEDS_AAP[t["id"]]:
                step["when"] = "tower_workflow_job_id is defined or awx_workflow_job_id is defined"
            tasks.append(step)
    tasks.append({"name": f"Run {role}/{task}",
                  "ansible.builtin.include_role": {"name": f"{NS}.{role}", "tasks_from": task}})
    tasks.append({"name": "Publish aap_ops_result and reports",
                  "ansible.builtin.include_role": {"name": f"{NS}.report"}})
    return [{
        "name": t["name"],
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": {"aap_ops_report_title": t["name"]},
        "tasks": tasks,
    }]


class Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _str(d, s):
    style = '"' if ("{{" in s or ":" in s or s.startswith("*")) else None
    return d.represent_scalar("tag:yaml.org,2002:str", s, style=style)


Dumper.add_representer(str, _str)
Dumper.ignore_aliases = lambda *a: True


def main():
    check = "--check" in sys.argv
    cat = yaml.safe_load((ROOT / "catalog.yml").read_text())
    stale = []
    for t in cat["templates"]:
        dom = cat["domains"][t["domain"]]["title"]
        path = ROOT / "playbooks" / t["domain"] / f"{t['id']}.yml"
        if t["id"] in HANDWRITTEN:
            if not path.exists():
                stale.append(f"missing handwritten {path}")
            continue
        plays = host_playbook(t, dom) if t["target"] == "hosts" else api_playbook(t, dom)
        body = header(t, dom) + yaml.dump(plays, Dumper=Dumper, sort_keys=False, width=100000, indent=2,
                                          default_flow_style=False)
        body = body.replace("\n- name:", "\n\n- name:").replace("---\n\n", "---\n")
        if check:
            if not path.exists() or path.read_text() != body:
                stale.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    if stale:
        print("\n".join(stale))
        sys.exit(1)
    print(f"{len(cat['templates'])} templates processed ({len(HANDWRITTEN)} handwritten)")


if __name__ == "__main__":
    main()

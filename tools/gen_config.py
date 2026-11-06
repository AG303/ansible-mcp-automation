#!/usr/bin/env python3
"""Generate AAP config-as-code (infra.aap_configuration vars) and docs/CATALOG.md from catalog.yml.

Outputs
  aap_config/vars/10_labels.yml            controller_labels
  aap_config/vars/20_credential_types.yml  controller_credential_types (ServiceNow basic auth, WinRM)
  aap_config/vars/30_credentials.yml       controller_credentials (secrets come from vault_* variables)
  aap_config/vars/40_execution_envs.yml    controller_execution_environments
  aap_config/vars/50_projects.yml          controller_projects
  aap_config/vars/60_inventories.yml       controller_inventories, controller_groups, controller_hosts
  aap_config/vars/70_job_templates.yml     controller_templates (one per catalog entry)
  aap_config/vars/80_workflows.yml         controller_workflows ("WF - <name>" for every changing template)
  aap_config/vars/90_teams_rbac.yml        aap_teams, controller_roles
  docs/CATALOG.md                          human / agent readable catalog
  execution-environment/requirements.yml   copy of collections/requirements.yml for ansible-builder

Usage: python3 tools/gen_config.py [--check]
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
VARS = ROOT / "aap_config" / "vars"

ORG = "{{ aap_ops_organization | default('Default') }}"
PROJECT = "AAP Ops"
EE = "AAP Ops EE"
INV_HOSTS = "AAP Ops - Managed Hosts"
INV_LOCAL = "AAP Ops - Localhost"
CRED = {
    "machine": "AAP Ops - Machine (Linux)",
    "windows": "AAP Ops - Windows (WinRM)",
    "api": "AAP Ops - Controller API",
    "ocp": "AAP Ops - OpenShift",
    "sn": "AAP Ops - ServiceNow",
}
TEAM_AGENTS = "MCP Agents"
TEAM_APPROVERS = "AAP Ops Approvers"
APPROVAL_TIMEOUT = "{{ aap_ops_approval_timeout | default(14400) }}"

# Workflow building blocks: launched by the WF workflows, not by agents directly.
WF_ONLY = {"sn_change_start", "sn_change_close"}
# Extra nodes around the main template inside its workflow.
NODE_VARS = {"sn_change_gate", "sn_change_start", "sn_change_close", "sn_incident_from_job",
             "patch_precheck", "patch_postcheck"}
PRE = {"patch_apply": "patch_precheck", "patch_reboot": "patch_precheck"}
POST = {"patch_apply": "patch_postcheck", "patch_reboot": "patch_postcheck", "mon_service_restart": None}
TIMEOUTS = {"patch_apply": 14400, "patch_reboot": 7200, "sec_compliance_remediate": 7200, "plat_backup": 7200,
            "sec_compliance_scan": 3600, "plat_config_apply": 3600, "job_cleanup_history": 3600}


def domain_label(cat, t):
    return cat["domains"][t["domain"]]["title"].split(",")[0].lower().replace(" ", "-")


def kind(t):
    if t["changes"]:
        return "changing"
    if t.get("writes") == "servicenow":
        return "servicenow-write"
    return "read-only"


def credentials(t):
    if t["target"] == "hosts":
        c = [CRED["machine"], CRED["windows"]]
        if t["id"].startswith("sn_"):
            c += [CRED["sn"], CRED["api"]]
        return c
    if t["target"] == "aap_api":
        return [CRED["api"]]
    if t["target"] == "openshift":
        return [CRED["ocp"]]
    return [CRED["sn"], CRED["api"]]


def survey_item(s):
    typ = s["type"]
    q = {"question_name": s["q"], "question_description": s.get("help", s["q"]), "variable": s["var"],
         "type": typ, "required": bool(s.get("required", False))}
    if "default" in s and s["default"] is not None:
        q["default"] = "\n".join(s["default"]) if typ == "multiselect" and isinstance(s["default"], list) else s["default"]
    if typ in ("multiplechoice", "multiselect"):
        q["choices"] = [str(c) for c in s["choices"]]
        if "default" in q:
            q["default"] = str(q["default"])
    elif typ in ("integer", "float"):
        q["min"] = s.get("min", 0)
        q["max"] = s.get("max", 1000000)
    else:
        q["min"] = s.get("min", 0)
        q["max"] = s.get("max", 4096 if typ == "textarea" else 1024)
    return q


def survey(name, items):
    return {"name": name, "description": f"Inputs for {name}", "spec": [survey_item(s) for s in items]}


def template(cat, t):
    hosts = t["target"] == "hosts"
    jt = {
        "name": t["name"],
        "description": t["description"][:512],
        "organization": ORG,
        "project": PROJECT,
        "playbook": f"playbooks/{t['domain']}/{t['id']}.yml",
        "inventory": INV_HOSTS if hosts else INV_LOCAL,
        "execution_environment": EE,
        "credentials": credentials(t),
        "labels": ["aap-ops", domain_label(cat, t), kind(t)] + (["mcp-agent"] if not t["changes"] and t["id"] not in WF_ONLY else []),
        "job_type": "run",
        "verbosity": 0,
        "timeout": TIMEOUTS.get(t["id"], 1800),
        "ask_limit_on_launch": hosts,
        # Only workflow-node templates accept free-form extra vars (node extra_data such as
        # sn_close_code). Everything else accepts survey answers only, so an agent cannot
        # override role defaults (gates, thresholds, become settings) at launch.
        "ask_variables_on_launch": t["id"] in NODE_VARS,
        "allow_simultaneous": not t["changes"],
        "survey_enabled": bool(t.get("survey")),
    }
    if hosts and t["changes"]:
        jt["forks"] = 25
    if t.get("survey"):
        jt["survey_spec"] = survey(t["name"], t["survey"])
    return jt


def workflow(cat, t, by_id):
    """WF - <name>: change gate -> approval -> change start -> [pre] -> template -> [post] -> close / incident."""
    nodes = []
    items = list(t.get("survey", []))
    for extra in (PRE.get(t["id"]), POST.get(t["id"])):
        if extra:
            items += [s for s in by_id[extra].get("survey", []) if s["var"] not in {i["var"] for i in items}]
    items = [{"var": "sn_change_number", "q": "ServiceNow change request number (approved, in window)",
              "type": "text", "required": True, "min": 10, "max": 40}] + items

    def node(ident, ujt=None, success=(), failure=(), extra=None, approval=None):
        n = {"identifier": ident, "all_parents_must_converge": False}
        if approval:
            n["approval_node"] = approval
        else:
            n["unified_job_template"] = ujt
        n["success_nodes"] = list(success)
        n["failure_nodes"] = list(failure)
        n["always_nodes"] = []
        if extra:
            n["extra_data"] = extra
        nodes.append(n)

    pre, post = PRE.get(t["id"]), POST.get(t["id"])
    main_next_ok = ["post"] if post else ["close_ok"]
    node("gate", by_id["sn_change_gate"]["name"], success=["approve"])
    node("approve", approval={"name": f"Approve: {t['name']}",
                              "description": "Change gate passed (approved CR, in window, CIs cover the limit). Approve to run.",
                              "timeout": APPROVAL_TIMEOUT}, success=["start"])
    node("start", by_id["sn_change_start"]["name"], success=["pre"] if pre else ["main"])
    if pre:
        node("pre", by_id[pre]["name"], success=["main"], failure=["close_fail"])
    node("main", t["name"], success=main_next_ok, failure=["close_fail", "incident"])
    if post:
        node("post", by_id[post]["name"], success=["close_ok"], failure=["close_fail", "incident"])
    node("close_ok", by_id["sn_change_close"]["name"], extra={"sn_close_code": "successful"})
    node("close_fail", by_id["sn_change_close"]["name"], extra={"sn_close_code": "unsuccessful"})
    node("incident", by_id["sn_incident_from_job"]["name"])
    return {
        "name": f"WF - {t['name']}",
        "description": ("APPROVAL REQUIRED. " + t["description"])[:512],
        "organization": ORG,
        "inventory": INV_HOSTS if t["target"] == "hosts" else INV_LOCAL,
        "ask_limit_on_launch": t["target"] == "hosts",
        "ask_variables_on_launch": False,
        "allow_simultaneous": False,
        "survey_enabled": True,
        "survey_spec": survey(f"WF - {t['name']}", items),
        "labels": ["aap-ops", domain_label(cat, t), "changing", "approval-required", "mcp-agent"],
        "destroy_current_nodes": True,
        "simplified_workflow_nodes": nodes,
    }


def doc(header, key, value):
    return header + yaml.safe_dump({key: value} if key else value, sort_keys=False, width=140, allow_unicode=True)


def build(cat):
    by_id = {t["id"]: t for t in cat["templates"]}
    out = {}
    H = "---\n# Generated by tools/gen_config.py from catalog.yml - do not edit by hand.\n"

    labels = {"aap-ops", "read-only", "changing", "servicenow-write", "approval-required", "mcp-agent"}
    labels |= {domain_label(cat, t) for t in cat["templates"]}
    out["10_labels.yml"] = doc(H, "controller_labels", [{"name": n, "organization": ORG} for n in sorted(labels)])

    out["20_credential_types.yml"] = doc(H, "controller_credential_types", [
        {"name": "ServiceNow ITSM (basic auth)", "kind": "cloud",
         "description": "ServiceNow instance URL and basic-auth integration user for servicenow.itsm and the sn_api role.",
         "inputs": {"fields": [
             {"id": "host", "type": "string", "label": "Instance URL (https://<instance>.service-now.com)"},
             {"id": "username", "type": "string", "label": "Username"},
             {"id": "password", "type": "string", "label": "Password", "secret": True}],
             "required": ["host", "username", "password"]},
         "injectors": {"env": {"SN_HOST": "{{ '{{' }} host {{ '}}' }}", "SN_USERNAME": "{{ '{{' }} username {{ '}}' }}",
                               "SN_PASSWORD": "{{ '{{' }} password {{ '}}' }}"}}},
        {"name": "AAP Ops WinRM", "kind": "cloud",
         "description": "Windows account injected as environment variables; the windows group maps them to ansible_user/ansible_password so the Linux machine credential stays separate.",
         "inputs": {"fields": [
             {"id": "username", "type": "string", "label": "Username (DOMAIN\\\\user or user@domain)"},
             {"id": "password", "type": "string", "label": "Password", "secret": True}],
             "required": ["username", "password"]},
         "injectors": {"env": {"AAP_OPS_WIN_USERNAME": "{{ '{{' }} username {{ '}}' }}",
                               "AAP_OPS_WIN_PASSWORD": "{{ '{{' }} password {{ '}}' }}"}}},
    ])

    out["30_credentials.yml"] = doc(H + "# Secrets come from vault_* variables (see aap_config/vault.example.yml).\n", "controller_credentials", [
        {"name": CRED["machine"], "organization": ORG, "credential_type": "Machine",
         "inputs": {"username": "{{ vault_linux_username }}", "ssh_key_data": "{{ vault_linux_ssh_key }}",
                    "become_method": "sudo"}},
        {"name": CRED["windows"], "organization": ORG, "credential_type": "AAP Ops WinRM",
         "inputs": {"username": "{{ vault_windows_username }}", "password": "{{ vault_windows_password }}"}},
        {"name": CRED["api"], "organization": ORG, "credential_type": "Red Hat Ansible Automation Platform",
         "description": "Least-privilege service account token for the API roles (job/RBAC audits, change gate evidence).",
         "inputs": {"host": "{{ aap_hostname }}", "oauth_token": "{{ vault_aap_ops_api_token }}",
                    "verify_ssl": "{{ aap_validate_certs | default(true) }}"}},
        {"name": CRED["ocp"], "organization": ORG, "credential_type": "OpenShift or Kubernetes API Bearer Token",
         "description": "Service account in the AAP namespace with get/patch on AnsibleAutomationPlatform and backup CRs.",
         "inputs": {"host": "{{ aap_ops_ocp_api_url }}", "bearer_token": "{{ vault_ocp_token }}", "verify_ssl": True}},
        {"name": CRED["sn"], "organization": ORG, "credential_type": "ServiceNow ITSM (basic auth)",
         "inputs": {"host": "{{ aap_ops_sn_instance_url }}", "username": "{{ vault_sn_username }}",
                    "password": "{{ vault_sn_password }}"}},
    ])

    out["40_execution_envs.yml"] = doc(H, "controller_execution_environments", [
        {"name": EE, "organization": ORG, "image": "{{ aap_ops_ee_image | default('registry.example.com/aap-ops/aap-ops-ee:1.0.0') }}",
         "pull": "missing", "description": "Built from execution-environment/execution-environment.yml"}])

    out["50_projects.yml"] = doc(H, "controller_projects", [
        {"name": PROJECT, "organization": ORG, "scm_type": "git",
         "scm_url": "{{ aap_ops_git_url | default('https://git.example.com/devops/aap26-mcp-automation.git') }}",
         "scm_branch": "{{ aap_ops_git_branch | default('main') }}", "scm_update_on_launch": False,
         "scm_clean": True, "default_environment": EE,
         "credential": "{{ aap_ops_git_credential | default(omit) }}",
         "description": "devops.aap_ops collection, playbooks and catalog"}])

    out["60_inventories.yml"] = (
        doc(H, "controller_inventories", [
            {"name": INV_HOSTS, "organization": ORG,
             "description": "Managed RHEL / Ubuntu / Windows hosts. Add an inventory source (Satellite, cloud, SCM) or hosts; keep Windows hosts in the windows group."},
            {"name": INV_LOCAL, "organization": ORG, "description": "Localhost only - API, OpenShift and ServiceNow templates"}])
        + yaml.safe_dump({"controller_groups": [
            {"name": "windows", "inventory": INV_HOSTS, "variables": {
                "ansible_connection": "winrm", "ansible_port": 5986, "ansible_winrm_transport": "ntlm",
                "ansible_winrm_server_cert_validation": "{{ '{{' }} aap_ops_winrm_cert_validation | default('validate') {{ '}}' }}",
                "ansible_user": "{{ '{{' }} lookup('ansible.builtin.env', 'AAP_OPS_WIN_USERNAME') {{ '}}' }}",
                "ansible_password": "{{ '{{' }} lookup('ansible.builtin.env', 'AAP_OPS_WIN_PASSWORD') {{ '}}' }}",
                "ansible_become": False}},
            {"name": "rhel", "inventory": INV_HOSTS}, {"name": "ubuntu", "inventory": INV_HOSTS}]},
            sort_keys=False, width=140)
        + yaml.safe_dump({"controller_hosts": [
            {"name": "localhost", "inventory": INV_LOCAL, "variables": {
                "ansible_connection": "local",
                "ansible_python_interpreter": "{{ '{{' }} ansible_playbook_python {{ '}}' }}"}}]},
            sort_keys=False, width=140))

    out["70_job_templates.yml"] = doc(H, "controller_templates", [template(cat, t) for t in cat["templates"]])

    wfs = [workflow(cat, t, by_id) for t in cat["templates"] if t["changes"]]
    out["80_workflows.yml"] = doc(H + "# Every changing template is reachable by agents only through its workflow.\n", "controller_workflows", wfs)

    agent_jts = [t["name"] for t in cat["templates"] if not t["changes"] and t["id"] not in WF_ONLY]
    out["90_teams_rbac.yml"] = (
        doc(H, "aap_teams", [
            {"name": TEAM_AGENTS, "organization": ORG,
             "description": "Service accounts used by AI agents through the AAP MCP server. Execute on read-only / ServiceNow-write templates and WF workflows only."},
            {"name": TEAM_APPROVERS, "organization": ORG,
             "description": "Humans who approve WF - workflow approval nodes."}])
        + yaml.safe_dump({"controller_roles": [
            {"team": TEAM_AGENTS, "job_templates": agent_jts, "role": "execute"},
            {"team": TEAM_AGENTS, "workflows": [w["name"] for w in wfs], "role": "execute"},
            {"team": TEAM_AGENTS, "inventories": [INV_HOSTS, INV_LOCAL], "role": "read"},
            {"team": TEAM_APPROVERS, "workflows": [w["name"] for w in wfs], "role": "approval"},
        ]}, sort_keys=False, width=140))

    out["../../docs/CATALOG.md"] = catalog_md(cat, by_id, wfs)
    # ansible-builder only reads files inside its context dir, so keep a copy of the
    # collection requirements next to execution-environment.yml.
    req = (ROOT / "collections" / "requirements.yml").read_text().split("\n")
    body = [ln for ln in req if not ln.startswith("#")]
    out["../../execution-environment/requirements.yml"] = (
        "# GENERATED by tools/gen_config.py from collections/requirements.yml - do not edit.\n" + "\n".join(body))
    return out


def catalog_md(cat, by_id, wfs):
    lines = ["# Template catalog", "",
             "Generated by `tools/gen_config.py` from `catalog.yml`. Agent entry point: read-only and ServiceNow-write",
             "templates are launched directly; changing templates only through their `WF - ` workflow (ServiceNow change",
             "gate, AAP approval node, change close and incident on failure).", ""]
    counts = {}
    for t in cat["templates"]:
        counts[kind(t)] = counts.get(kind(t), 0) + 1
    lines += [f"Total: {len(cat['templates'])} job templates ({', '.join(f'{v} {k}' for k, v in counts.items())}) and {len(wfs)} approval workflows.", ""]
    for d, meta in cat["domains"].items():
        ts = [t for t in cat["templates"] if t["domain"] == d]
        lines += [f"## {meta['title']} ({meta['code']}, {len(ts)})", "",
                  "| Template | Type | Agent entry point | Target / OS | Survey variables | Playbook |",
                  "|---|---|---|---|---|---|"]
        for t in ts:
            entry = f"WF - {t['name']}" if t["changes"] else ("workflow node only" if t["id"] in WF_ONLY else "job template")
            tgt = t["target"] + (" (" + ", ".join(t.get("os", [])) + ")" if t["target"] == "hosts" else "")
            sv = ", ".join(f"`{s['var']}`" for s in t.get("survey", [])) or "-"
            lines.append(f"| **{t['name']}**<br>{t['description']} | {kind(t)} | {entry} | {tgt} | {sv} | `playbooks/{d}/{t['id']}.yml` |")
        lines.append("")
    return "\n".join(lines)


def main():
    check = "--check" in sys.argv
    cat = yaml.safe_load((ROOT / "catalog.yml").read_text())
    VARS.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, body in build(cat).items():
        p = (VARS / name).resolve()
        if check:
            if not p.exists() or p.read_text() != body:
                stale.append(str(p))
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    if stale:
        print("stale:\n" + "\n".join(stale))
        sys.exit(1)
    print("config generated" if not check else "config up to date")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Referential checks for aap_config/vars (run after tools/gen_config.py).

Verifies every job template, workflow node and RBAC entry points at objects that the
config defines, that playbooks exist, surveys are well formed and that the agent team
cannot execute changing job templates directly.
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
cfg = {}
for f in sorted((ROOT / "aap_config" / "vars").glob("*.yml")):
    cfg.update(yaml.safe_load(f.read_text()) or {})
cat = yaml.safe_load((ROOT / "catalog.yml").read_text())
errors = []
names = lambda k: {o["name"] for o in cfg.get(k, [])}
creds, invs, ees, projs, labels = (names(k) for k in ("controller_credentials", "controller_inventories",
                                                      "controller_execution_environments", "controller_projects", "controller_labels"))
ctypes = names("controller_credential_types") | {"Machine", "Red Hat Ansible Automation Platform", "OpenShift or Kubernetes API Bearer Token"}
jts = {j["name"]: j for j in cfg["controller_templates"]}
wfs = {w["name"]: w for w in cfg["controller_workflows"]}
changing = {t["name"] for t in cat["templates"] if t["changes"]}

for c in cfg["controller_credentials"]:
    if c["credential_type"] not in ctypes:
        errors.append(f"credential {c['name']}: unknown type {c['credential_type']}")
for j in jts.values():
    for c in j["credentials"]:
        if c not in creds:
            errors.append(f"{j['name']}: unknown credential {c}")
    if j["inventory"] not in invs or j["execution_environment"] not in ees or j["project"] not in projs:
        errors.append(f"{j['name']}: unknown inventory/EE/project")
    if not (ROOT / j["playbook"]).exists():
        errors.append(f"{j['name']}: missing playbook {j['playbook']}")
    for lb in j["labels"]:
        if lb not in labels:
            errors.append(f"{j['name']}: unknown label {lb}")
    if len([c for c in j["credentials"] if c == "AAP Ops - Machine (Linux)"]) > 1:
        errors.append(f"{j['name']}: more than one machine credential")
    for q in j.get("survey_spec", {}).get("spec", []):
        if q["type"] == "multiplechoice" and "default" in q and q["default"] not in q["choices"]:
            errors.append(f"{j['name']}: default {q['default']!r} of {q['variable']} not in choices")
        if q["type"] not in {"text", "textarea", "password", "integer", "float", "multiplechoice", "multiselect"}:
            errors.append(f"{j['name']}: bad survey type {q['type']}")
for w in wfs.values():
    ids = {n["identifier"] for n in w["simplified_workflow_nodes"]}
    approvals = [n for n in w["simplified_workflow_nodes"] if "approval_node" in n]
    if len(approvals) != 1:
        errors.append(f"{w['name']}: expected exactly one approval node")
    for n in w["simplified_workflow_nodes"]:
        if "unified_job_template" in n and n["unified_job_template"] not in jts:
            errors.append(f"{w['name']}/{n['identifier']}: unknown JT {n['unified_job_template']}")
        for k in ("success_nodes", "failure_nodes", "always_nodes"):
            for x in n.get(k, []):
                if x not in ids:
                    errors.append(f"{w['name']}/{n['identifier']}: dangling {k} {x}")
        if "extra_data" in n:
            jt = jts[n["unified_job_template"]]
            if not jt["ask_variables_on_launch"]:
                errors.append(f"{w['name']}/{n['identifier']}: extra_data on JT without ask_variables_on_launch")
    first = w["simplified_workflow_nodes"][0]
    if first["unified_job_template"] != "SN - Change gate":
        errors.append(f"{w['name']}: first node must be the change gate")
    if w["survey_spec"]["spec"][0]["variable"] != "sn_change_number" or not w["survey_spec"]["spec"][0]["required"]:
        errors.append(f"{w['name']}: sn_change_number must be the first required survey question")
for t in changing:
    if f"WF - {t}" not in wfs:
        errors.append(f"changing template {t} has no workflow")
for r in cfg["controller_roles"]:
    for j in r.get("job_templates", []):
        if j not in jts:
            errors.append(f"role {r}: unknown JT {j}")
        if r["team"] == "MCP Agents" and j in changing:
            errors.append(f"MCP Agents can execute changing JT {j} directly")
    for w in r.get("workflows", []):
        if w not in wfs:
            errors.append(f"role: unknown workflow {w}")
print(f"{len(jts)} job templates, {len(wfs)} workflows, {len(cfg['controller_roles'])} role entries checked")
if errors:
    print("\n".join(errors))
    sys.exit(1)
print("OK")

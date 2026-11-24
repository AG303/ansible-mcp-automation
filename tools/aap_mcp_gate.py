#!/usr/bin/env python3
"""Approval gate for AAP MCP tool calls made by editor agents (VS Code Copilot and Cursor).

The editor runs this script as a hook before every MCP tool call (and, for shell commands
that look like direct AAP access, before the command). It reads the hook payload on stdin
and prints one decision on stdout:

  allow  read-only tools, and launches of read-only catalog job templates that pass every check
  ask    launches that write ServiceNow records, and every "WF - " workflow launch. The editor
         shows the call to the user first. After launch, the workflow still has to pass the
         ServiceNow change gate and wait for a human in "AAP Ops Approvers" to approve it in AAP.
  deny   everything else: tools outside the allowlist, the discover endpoint, bare changing
         templates, unknown template IDs, launches without a narrow limit, survey answers that
         the template doesn't ask for, launch-time overrides, workflows without a change number,
         and shell commands that would reach AAP directly.

The script uses only the standard library, so it runs under any python3 that the editor finds.
Policy comes from agent/aap_mcp_policy.json, which tools/gen_agent_config.py generates from catalog.yml.
Template IDs come from .aap-mcp/template_ids.json, which tools/aap_mcp_sync.py writes after it
checks the agent's token.

Usage (configured in .cursor/hooks.json and .github/hooks/aap-mcp-gate.json):
  python3 tools/aap_mcp_gate.py --client cursor-mcp     Cursor beforeMCPExecution
  python3 tools/aap_mcp_gate.py --client cursor-shell   Cursor beforeShellExecution
  python3 tools/aap_mcp_gate.py --client copilot        VS Code / Copilot preToolUse or PreToolUse

Environment
  AAP_MCP_GATE_PASSTHROUGH   decision for MCP servers that are not AAP in Cursor (default "ask";
                             Copilot always returns no decision for them)
  AAP_MCP_GATE_ROOT          repository root (default: parent of this script's directory)
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("AAP_MCP_GATE_ROOT") or Path(__file__).resolve().parent.parent)
POLICY_FILE = ROOT / "agent" / "aap_mcp_policy.json"
SHELL_TOOLS = {"bash", "powershell", "shell", "run_in_terminal", "runinterminal", "terminal", "execute"}
PREFIX_TOKENS = {"", "mcp", "aap", "jobs", "monitor", "inventory", "controller", "gateway", "eda"}
ALLOW, ASK, DENY, NONE = "allow", "ask", "deny", "none"


class Gate:
    def __init__(self, policy, id_map=None, now=None):
        self.p = policy
        self.id_map = id_map
        self.now = now or time.time()
        self.read = {t for tools in policy["read_tools"].values() for t in tools}
        self.launch = {t for tools in policy["launch_tools"].values() for t in tools}
        self.known = self.read | self.launch | set(policy["deny_reasons"])
        self.servers = set(policy["servers"])
        self.toolsets = {v["toolset"] for v in policy["servers"].values()}

    # ------------------------------------------------------------------ helpers
    def resolve_tool(self, raw):
        """Return (is_aap, tool) for a client tool name such as 'aap-jobs/jobs_list'."""
        raw = (raw or "").strip()
        lowered = raw.lower()
        server_hit = any(s in lowered or s.replace("-", "_") in lowered for s in self.servers)
        for name in sorted(self.known, key=len, reverse=True):
            if not lowered.endswith(name):
                continue
            prefix = lowered[: len(lowered) - len(name)]
            if prefix and prefix[-1] not in "/._:-":
                continue
            if all(tok in PREFIX_TOKENS for tok in re.split(r"[/._:\-]+", prefix)):
                return True, name
        if server_hit:
            return True, raw
        return False, raw

    def is_aap_server(self, server, url):
        if server in self.servers:
            return True
        m = re.search(r"/mcp/([a-z_]+)/?$", url or "")
        return bool(m and (m.group(1) in self.toolsets or m.group(1) in ("discover", "user_management",
                                                                          "security_compliance",
                                                                          "platform_configuration",
                                                                          "content_discovery")))

    @staticmethod
    def parse_args(raw):
        if raw is None or raw == "":
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            val = json.loads(raw)
            if isinstance(val, dict):
                return val
        raise ValueError("tool arguments are not a JSON object")

    def broad_limit(self, limit):
        terms = [t.strip() for t in re.split(r"[,:]", limit or "") if t.strip()]
        if not terms:
            return True
        if any(t.lower() in self.p["broad_limits"] for t in terms):
            return True
        return all(t[0] in "!&" for t in terms)

    def template_name(self, kind, ident):
        if self.id_map is None:
            return None, ("Template IDs are not synced. Run `python3 tools/aap_mcp_sync.py` with the agent's token "
                          "(it checks the token's RBAC first), then retry.")
        table = self.id_map.get(kind, {})
        name = table.get(str(ident))
        if name is None:
            return None, (f"ID {ident} is not an agent-launchable catalog template. Look it up with "
                          f"{'job_templates_list' if kind == 'job_templates' else 'workflow_job_templates_list'} "
                          "(filter by label mcp-agent), or re-run tools/aap_mcp_sync.py if templates were recreated.")
        return name, None

    def stale(self):
        if not self.id_map:
            return False
        try:
            gen = datetime.strptime(self.id_map["generated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            return True
        return (self.now - gen.timestamp()) > self.p.get("id_map_max_age_days", 7) * 86400

    # ------------------------------------------------------------------ rules
    def check_body(self, args, spec, label):
        """Validate {id, requestBody:{limit, extra_vars}} against the template's survey and limit rules."""
        extra_keys = set(args) - {"id", "requestBody"}
        if extra_keys:
            return None, f"Unexpected arguments {sorted(extra_keys)} for {label}."
        body = args.get("requestBody") or {}
        if isinstance(body, str):
            body = self.parse_args(body)
        bad = set(body) - set(self.p["launch_body_keys"])
        if bad:
            return None, (f"Launch-time overrides {sorted(bad)} are not allowed. Send only "
                          f"{self.p['launch_body_keys']} (survey answers go in extra_vars).")
        ev = body.get("extra_vars") or {}
        if isinstance(ev, str):
            try:
                ev = json.loads(ev) if ev.strip() else {}
            except ValueError:
                return None, "Send extra_vars as a JSON object of survey answers."
        if not isinstance(ev, dict):
            return None, "Send extra_vars as a JSON object of survey answers."
        unknown = sorted(set(ev) - set(spec["survey_vars"]))
        if unknown:
            return None, (f"{label} does not ask for {unknown}. Allowed survey variables: "
                          f"{spec['survey_vars'] or 'none'}.")
        missing = sorted(v for v in spec.get("required_vars", []) if ev.get(v) in (None, ""))
        if missing:
            return None, f"{label} needs the survey answers {missing}."
        limit = body.get("limit")
        if spec["requires_limit"]:
            if not isinstance(limit, str) or self.broad_limit(limit):
                return None, (f"{label} runs on managed hosts. Set requestBody.limit to the narrowest host "
                              "pattern that fits (not empty, 'all' or '*').")
        elif limit not in (None, ""):
            return None, f"{label} runs on localhost and does not take a limit."
        return {"extra_vars": ev, "limit": limit}, None

    def launch_job_template(self, args):
        ident = args.get("id")
        if ident in (None, ""):
            return DENY, "job_templates_launch_create needs the job template id.", {}
        name, err = self.template_name("job_templates", ident)
        if err:
            return DENY, err, {"template_id": ident}
        if name in self.p["blocked_templates"]:
            return DENY, (f"'{name}' changes systems or only runs inside a workflow. "
                          f"Launch 'WF - {name}' with an approved change number."), {"template": name}
        spec = self.p["job_templates"].get(name)
        if spec is None:
            return DENY, f"'{name}' is not in the agent catalog.", {"template": name}
        ok, err = self.check_body(args, spec, f"'{name}'")
        if err:
            return DENY, err, {"template": name}
        meta = {"template": name, "limit": ok["limit"], "survey_vars": sorted(ok["extra_vars"])}
        if spec["kind"] == "servicenow-write":
            return ASK, (f"'{name}' writes ServiceNow records (no system change). "
                         f"Confirm to launch{' on ' + ok['limit'] if ok['limit'] else ''}."), meta
        if self.stale():
            return ASK, (f"Read-only '{name}', but the template ID map is more than "
                         f"{self.p.get('id_map_max_age_days', 7)} days old. Confirm, then re-run tools/aap_mcp_sync.py."), meta
        return ALLOW, f"Read-only catalog template '{name}'.", meta

    def launch_workflow(self, args):
        ident = args.get("id")
        if ident in (None, ""):
            return DENY, "workflow_job_templates_launch_create needs the workflow id.", {}
        name, err = self.template_name("workflow_job_templates", ident)
        if err:
            return DENY, err, {"template_id": ident}
        spec = self.p["workflows"].get(name)
        if spec is None:
            return DENY, f"'{name}' is not an approval-gated catalog workflow.", {"template": name}
        ok, err = self.check_body(args, spec, f"'{name}'")
        if err:
            if "sn_change_number" in err:
                err += (" If there is no change yet, launch 'SN - Create change request' and wait for CAB "
                        "approval and the planned window.")
            return DENY, err, {"template": name}
        chg = str(ok["extra_vars"].get("sn_change_number", ""))
        if not re.match(self.p["change_number_regex"], chg):
            return DENY, (f"sn_change_number {chg!r} is not a ServiceNow change number (CHG followed by digits). "
                          "Launch 'SN - Create change request' first if there is none."), {"template": name}
        meta = {"template": name, "limit": ok["limit"], "change": chg, "survey_vars": sorted(ok["extra_vars"])}
        where = f" on {ok['limit']}" if ok["limit"] else ""
        return ASK, (f"CHANGES SYSTEMS: '{name}'{where} under {chg}. If you confirm, AAP checks the change "
                     f"(approved, in window, CIs cover the limit) and then waits for a human in "
                     f"'{self.p['approver_team']}' to approve before anything runs."), meta

    def mcp(self, raw_tool, raw_args, server=None, url=None):
        """Decide one MCP tool call. Returns (decision, reason, meta)."""
        is_aap, tool = self.resolve_tool(raw_tool)
        if server is not None or url is not None:
            is_aap = self.is_aap_server(server, url)
            if is_aap and re.search(r"/mcp/discover/?$", url or ""):
                return DENY, self.p["deny_reasons"]["discover"], {"tool": tool}
            if is_aap and server not in self.servers:
                return DENY, (f"MCP server '{server}' points at AAP but is not one of the gated servers "
                              f"{sorted(self.servers)}. Use the generated .cursor/mcp.json."), {"tool": tool}
        if not is_aap:
            return NONE, "", {"tool": tool}
        meta = {"tool": tool}
        if tool in self.p["deny_reasons"]:
            return DENY, self.p["deny_reasons"][tool], meta
        if tool in self.read:
            return ALLOW, f"Read-only AAP tool {tool}.", meta
        if tool not in self.launch:
            return DENY, (f"AAP tool '{tool}' is not on the agent allowlist (agent/aap_mcp_policy.json). "
                          "Use a catalog job template or WF workflow."), meta
        try:
            args = self.parse_args(raw_args)
        except ValueError as exc:
            return DENY, f"Could not read the tool arguments: {exc}.", meta
        if tool == "job_templates_launch_create":
            d, r, m = self.launch_job_template(args)
        else:
            d, r, m = self.launch_workflow(args)
        meta.update(m)
        return d, r, meta

    def shell(self, command):
        s = self.p["shell"]
        cmd = command or ""
        if not re.search(s["matcher"], cmd):
            return NONE, "", {}
        why = None
        if re.search(s["token_regex"], cmd):
            why = "it references an AAP token variable"
        elif re.search(s["cli_regex"], cmd):
            why = "it runs the awx CLI or applies AAP config-as-code"
        elif re.search(s["client_regex"], cmd) and re.search(s["aap_path_regex"], cmd):
            why = "it calls the AAP API or MCP endpoint directly"
        if why:
            return DENY, (f"Blocked shell command: {why}. Agents reach AAP only through the gated MCP tools "
                          "(aap-jobs, aap-monitor, aap-inventory)."), {"command": cmd[:200]}
        return NONE, "", {}


# ---------------------------------------------------------------------- I/O
def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None


def audit(policy, client, decision, reason, meta):
    if decision == NONE:
        return
    try:
        path = ROOT / policy["audit_log"]
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "client": client,
               "decision": decision, "reason": reason}
        rec.update({k: v for k, v in meta.items() if k != "extra_vars"})
        with path.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError as exc:  # never block on logging problems, but say so
        print(f"aap_mcp_gate: audit log not written: {exc}", file=sys.stderr)


def emit(client, decision, reason, passthrough):
    if client.startswith("cursor"):
        if decision == NONE:
            if client == "cursor-shell":
                decision, reason = ALLOW, ""
            else:
                decision, reason = passthrough, "Not an AAP MCP server; normal approval applies."
        out = {"permission": decision}
        if reason:
            out.update(user_message=reason, agent_message=reason)
        return out
    if decision == NONE:
        return {}
    return {"permissionDecision": decision, "permissionDecisionReason": reason}


def decide(client, event, gate):
    if client == "cursor-mcp":
        if not event.get("mcp_server_name"):
            return DENY, "MCP call without a server name; blocked.", {"tool": event.get("tool_name")}
        return gate.mcp(event.get("tool_name"), event.get("tool_input"),
                        server=event.get("mcp_server_name") or "", url=event.get("mcp_server_url") or event.get("url"))
    if client == "cursor-shell":
        return gate.shell(event.get("command"))
    # Copilot: camelCase (preToolUse) or snake_case (PreToolUse) payload
    tool = event.get("toolName", event.get("tool_name"))
    args = event.get("toolArgs", event.get("tool_input"))
    if not isinstance(tool, str):
        raise ValueError("payload has no toolName")
    if tool.lower() in SHELL_TOOLS or tool == "Bash":
        try:
            parsed = Gate.parse_args(args)
        except ValueError:
            parsed = {"command": str(args)}
        return gate.shell(parsed.get("command") or parsed.get("script") or "")
    return gate.mcp(tool, args)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--client", required=True, choices=["cursor-mcp", "cursor-shell", "copilot"])
    a = ap.parse_args(argv)
    passthrough = os.environ.get("AAP_MCP_GATE_PASSTHROUGH", ASK)
    if passthrough not in (ALLOW, ASK, DENY):
        passthrough = ASK
    policy = None
    try:
        policy = load_json(POLICY_FILE)
        if policy is None:
            raise RuntimeError(f"{POLICY_FILE} is missing; run python3 tools/gen_agent_config.py")
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            raise ValueError("hook payload is not a JSON object")
        gate = Gate(policy, load_json(ROOT / policy["id_map_file"]))
        decision, reason, meta = decide(a.client, event, gate)
        meta = dict(meta, raw_tool=event.get("tool_name") or event.get("toolName"))
    except Exception as exc:  # fail closed
        decision, reason, meta = DENY, f"AAP MCP gate error, call blocked: {exc}", {}
        print(json.dumps(emit(a.client, decision, reason, passthrough)))
        if policy:
            audit(policy, a.client, decision, reason, meta)
        return 2 if a.client.startswith("cursor") else 0
    audit(policy, a.client, decision, reason, meta)
    print(json.dumps(emit(a.client, decision, reason, passthrough)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

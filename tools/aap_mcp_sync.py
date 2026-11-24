#!/usr/bin/env python3
"""Preflight the editor agent's AAP token and write the template ID map used by tools/aap_mcp_gate.py.

Checks (FAIL stops the map from being written, so the gate keeps denying launches):
  token user   not a superuser, member of the agent team, not a member of the approver team
  RBAC         the token cannot start any changing (or workflow-only) job template directly
  workflows    every agent-visible "WF - " workflow still has an approval node
  catalog      which catalog templates the token can see and launch (WARN if some are missing)
  MCP          each gated toolset answers initialize + tools/list, the allowlisted tools exist,
               and the launch tools are exposed (ALLOW_WRITE_OPERATIONS=true on the MCP server)

Environment
  AAP_MCP_TOKEN   token of the dedicated agent user (required)
  AAP_URL         platform gateway URL, e.g. https://aap.example.com (default: AAP_MCP_URL without its port)
  AAP_MCP_URL     MCP server base URL, e.g. https://aap.example.com:8448
  AAP_MCP_INSECURE=1 or --insecure   skip TLS verification (lab only); SSL_CERT_FILE sets a CA bundle

Usage
  python3 tools/aap_mcp_sync.py                 # check, then write .aap-mcp/template_ids.json
  python3 tools/aap_mcp_sync.py --check-only    # check without writing the map
  python3 tools/aap_mcp_sync.py --skip-mcp      # skip the MCP endpoint checks
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("AAP_MCP_GATE_ROOT") or Path(__file__).resolve().parent.parent)
POLICY_FILE = ROOT / "agent" / "aap_mcp_policy.json"
MCP_PROTOCOL = "2025-06-18"


class Report:
    def __init__(self):
        self.fails, self.warns = 0, 0

    def line(self, level, msg):
        if level == "FAIL":
            self.fails += 1
        elif level == "WARN":
            self.warns += 1
        print(f"[{level}] {msg}")


class Client:
    def __init__(self, token, insecure=False, timeout=30):
        self.token = token
        self.timeout = timeout
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def request(self, method, url, body=None, headers=None):
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        ctx = self.ctx if url.startswith("https") else None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace")

    def get(self, url):
        status, _, text = self.request("GET", url)
        if status != 200:
            raise RuntimeError(f"GET {url} returned HTTP {status}: {text[:200]}")
        return json.loads(text)

    def get_all(self, base, url):
        out = []
        while url:
            page = self.get(url)
            out.extend(page.get("results", []))
            nxt = page.get("next")
            url = urllib.parse.urljoin(base, nxt) if nxt else None
        return out


def default_gateway(mcp_url):
    p = urllib.parse.urlparse(mcp_url)
    return f"{p.scheme}://{p.hostname}"


def check_user(c, gw, policy, rep):
    me = c.get(f"{gw}/api/gateway/v1/me/")
    if "results" in me:
        if not me["results"]:
            raise RuntimeError("/api/gateway/v1/me/ returned no user")
        me = me["results"][0]
    user = me.get("username", "?")
    if me.get("is_superuser"):
        rep.line("FAIL", f"token user '{user}' is a superuser. Issue the token for a dedicated agent user.")
    else:
        rep.line("OK", f"token user '{user}' is not a superuser")
    teams = {t.get("name") for t in c.get_all(gw, f"{gw}/api/gateway/v1/users/{me['id']}/teams/?page_size=200")}
    if policy["approver_team"] in teams:
        rep.line("FAIL", f"'{user}' is in '{policy['approver_team']}', so the agent could approve its own changes.")
    else:
        rep.line("OK", f"'{user}' is not in '{policy['approver_team']}'")
    if policy["agent_team"] in teams:
        rep.line("OK", f"'{user}' is in '{policy['agent_team']}'")
    else:
        rep.line("WARN", f"'{user}' is not in '{policy['agent_team']}'; catalog templates may not be visible.")
    return user


def check_templates(c, gw, policy, rep):
    api = f"{gw}/api/controller/v2"
    jts = c.get_all(gw, f"{api}/job_templates/?page_size=200")
    wfs = c.get_all(gw, f"{api}/workflow_job_templates/?page_size=200")
    can = lambda o: bool(o.get("summary_fields", {}).get("user_capabilities", {}).get("start"))
    id_map = {"job_templates": {}, "workflow_job_templates": {}}

    for o in jts:
        name = o.get("name")
        if name in policy["blocked_templates"] and can(o):
            rep.line("FAIL", f"token can launch '{name}' (id {o['id']}) directly. It changes systems or only runs "
                             f"inside a workflow; remove Execute for '{policy['agent_team']}'.")
        if name in policy["job_templates"] and can(o):
            id_map["job_templates"][str(o["id"])] = name
    for o in wfs:
        name = o.get("name")
        if name not in policy["workflows"] or not can(o):
            continue
        nodes = c.get_all(gw, f"{api}/workflow_job_templates/{o['id']}/workflow_nodes/?page_size=200")
        approvals = [n for n in nodes if n.get("summary_fields", {}).get("unified_job_template", {})
                     .get("unified_job_type") == "workflow_approval"]
        if not approvals:
            rep.line("FAIL", f"workflow '{name}' (id {o['id']}) has no approval node; it was changed outside config-as-code.")
            continue
        id_map["workflow_job_templates"][str(o["id"])] = name

    for kind, catalog in (("job_templates", policy["job_templates"]), ("workflow_job_templates", policy["workflows"])):
        found = set(id_map[kind].values())
        missing = sorted(set(catalog) - found)
        level = "OK" if not missing else "WARN"
        rep.line(level, f"{len(found)}/{len(catalog)} catalog {kind.replace('_', ' ')} launchable by the token"
                        + (f"; missing: {', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}" if missing else ""))
    return id_map


def parse_mcp(headers, text):
    ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    if "text/event-stream" in ctype:
        msgs = [json.loads(line[5:].strip()) for line in text.splitlines()
                if line.startswith("data:") and line[5:].strip()]
        return next((m for m in reversed(msgs) if "result" in m or "error" in m), msgs[-1] if msgs else {})
    return json.loads(text) if text.strip() else {}


def check_mcp(c, mcp_url, policy, rep):
    accept = {"Accept": "application/json, text/event-stream"}
    for server, spec in policy["servers"].items():
        url = f"{mcp_url.rstrip('/')}/mcp/{spec['toolset']}"
        try:
            st, hd, tx = c.request("POST", url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": MCP_PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "aap-mcp-sync", "version": "1.0"}}}, accept)
            if st != 200:
                rep.line("FAIL", f"{server}: initialize returned HTTP {st}: {tx[:160]}")
                continue
            sid = next((v for k, v in hd.items() if k.lower() == "mcp-session-id"), None)
            h = dict(accept, **({"Mcp-Session-Id": sid} if sid else {}), **{"MCP-Protocol-Version": MCP_PROTOCOL})
            c.request("POST", url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, h)
            st, hd, tx = c.request("POST", url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, h)
            msg = parse_mcp(hd, tx) if st == 200 else {}
            if "result" not in msg:
                rep.line("FAIL", f"{server}: tools/list failed (HTTP {st}): {tx[:160]}")
                continue
            exposed = {t["name"] for t in msg["result"].get("tools", [])}
            want_read = set(policy["read_tools"].get(server, []))
            want_launch = set(policy["launch_tools"].get(server, []))
            miss = sorted(want_read - exposed)
            rep.line("OK" if not miss else "WARN",
                     f"{server} ({spec['toolset']}): {len(exposed)} tools exposed, "
                     f"{len(want_read & exposed)}/{len(want_read)} allowlisted read tools present"
                     + (f"; missing {miss}" if miss else ""))
            if want_launch:
                miss_l = sorted(want_launch - exposed)
                if miss_l:
                    rep.line("WARN", f"{server}: launch tools {miss_l} not exposed. Set ALLOW_WRITE_OPERATIONS=true on "
                                     "the MCP server if agents should launch catalog templates.")
                else:
                    rep.line("OK", f"{server}: launch tools exposed (write operations enabled; the gate controls them)")
            extra = sorted(exposed - want_read - want_launch)
            if extra:
                rep.line("INFO", f"{server}: {len(extra)} other tools exposed; the editor hides or the gate denies them")
        except (OSError, ValueError, RuntimeError) as exc:
            rep.line("FAIL", f"{server}: {exc}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check-only", action="store_true", help="run the checks without writing the ID map")
    ap.add_argument("--skip-mcp", action="store_true", help="skip the MCP endpoint checks")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (lab only)")
    a = ap.parse_args(argv)

    token = os.environ.get("AAP_MCP_TOKEN")
    mcp_url = os.environ.get("AAP_MCP_URL", "")
    gw = (os.environ.get("AAP_URL") or (default_gateway(mcp_url) if mcp_url else "")).rstrip("/")
    if not token or not gw:
        print("Set AAP_MCP_TOKEN and AAP_URL (or AAP_MCP_URL).", file=sys.stderr)
        return 2
    if not mcp_url and not a.skip_mcp:
        print("Set AAP_MCP_URL, or pass --skip-mcp.", file=sys.stderr)
        return 2
    policy = json.loads(POLICY_FILE.read_text())
    c = Client(token, insecure=a.insecure or os.environ.get("AAP_MCP_INSECURE") == "1")
    rep = Report()
    try:
        user = check_user(c, gw, policy, rep)
        id_map = check_templates(c, gw, policy, rep)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        rep.line("FAIL", f"AAP API: {exc}")
        user, id_map = None, None
    if not a.skip_mcp:
        check_mcp(c, mcp_url, policy, rep)

    out = ROOT / policy["id_map_file"]
    if rep.fails:
        print(f"\n{rep.fails} FAIL, {rep.warns} WARN. Template ID map not written; the gate keeps denying launches.")
        return 1
    if not a.check_only:
        out.parent.mkdir(parents=True, exist_ok=True)
        data = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "aap_url": gw, "user": user}
        data.update(id_map)
        out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {out.relative_to(ROOT)}: {len(id_map['job_templates'])} job templates, "
              f"{len(id_map['workflow_job_templates'])} workflows")
    print(f"0 FAIL, {rep.warns} WARN")
    return 0


if __name__ == "__main__":
    sys.exit(main())

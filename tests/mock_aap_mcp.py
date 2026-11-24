#!/usr/bin/env python3
"""AAP 2.6 gateway/controller + AAP MCP server mock for tests/test_agent_gate.py.

Serves the endpoints tools/aap_mcp_sync.py reads:
  GET  /api/gateway/v1/me/, /api/gateway/v1/users/<id>/teams/
  GET  /api/controller/v2/job_templates/, /workflow_job_templates/, /workflow_job_templates/<id>/workflow_nodes/
  POST /mcp/<toolset>   JSON-RPC initialize, notifications/initialized, tools/list (Mcp-Session-Id, JSON or SSE)

Templates come from agent/aap_mcp_policy.json. RBAC matches aap_config: the agent can start read-only
templates and WF workflows, not changing templates.

Scenarios (second argument): ok, superuser, approver, bad_rbac, no_approval, no_write, sse
Usage: python3 tests/mock_aap_mcp.py <port> [scenario]
"""
import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
POLICY = json.loads((ROOT / "agent" / "aap_mcp_policy.json").read_text())
EXTRA_TOOLS = ["job_templates_create", "jobs_cancel_create", "ad_hoc_commands_create", "users_list"]


def build(scenario):
    jts, wfs = [], []
    names = list(POLICY["job_templates"]) + list(POLICY["blocked_templates"])
    for i, name in enumerate(names, start=10):
        start = name in POLICY["job_templates"] or (scenario == "bad_rbac" and name == "MON - Disk cleanup")
        jts.append({"id": i, "name": name, "summary_fields": {"user_capabilities": {"start": start}}})
    for i, name in enumerate(POLICY["workflows"], start=200):
        wfs.append({"id": i, "name": name, "summary_fields": {"user_capabilities": {"start": True}}})
    return jts, wfs


class Handler(BaseHTTPRequestHandler):
    scenario = "ok"
    sessions = set()

    def log_message(self, *args):
        pass

    def send(self, code, body, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {"Content-Type": "application/json"}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authed(self):
        if self.headers.get("Authorization") != "Bearer agent-token":
            self.send(401, {"detail": "Authentication credentials were not provided."})
            return False
        return True

    def page(self, items):
        q = parse_qs(urlparse(self.path).query)
        size = int(q.get("page_size", ["25"])[0])
        n = int(q.get("page", ["1"])[0])
        chunk = items[(n - 1) * size: n * size]
        base = urlparse(self.path).path
        nxt = f"{base}?page_size={size}&page={n + 1}" if n * size < len(items) else None
        return {"count": len(items), "next": nxt, "previous": None, "results": chunk}

    def do_GET(self):
        if not self.authed():
            return
        s = self.scenario
        path = urlparse(self.path).path
        jts, wfs = build(s)
        if path == "/api/gateway/v1/me/":
            return self.send(200, self.page([{"id": 7, "username": "mcp-agent", "is_superuser": s == "superuser"}]))
        if path == "/api/gateway/v1/users/7/teams/":
            teams = [{"id": 3, "name": POLICY["agent_team"]}]
            if s == "approver":
                teams.append({"id": 4, "name": POLICY["approver_team"]})
            return self.send(200, self.page(teams))
        if path == "/api/controller/v2/job_templates/":
            return self.send(200, self.page(jts))
        if path == "/api/controller/v2/workflow_job_templates/":
            return self.send(200, self.page(wfs))
        if path.startswith("/api/controller/v2/workflow_job_templates/") and path.endswith("/workflow_nodes/"):
            wid = int(path.split("/")[5])
            kinds = ["job", "workflow_approval", "job", "job"]
            if s == "no_approval" and wid == 200:
                kinds = ["job", "job", "job"]
            nodes = [{"id": wid * 10 + k, "summary_fields": {"unified_job_template": {"unified_job_type": t}}}
                     for k, t in enumerate(kinds)]
            return self.send(200, self.page(nodes))
        self.send(404, {"detail": "Not found."})

    def do_POST(self):
        if not self.authed():
            return
        path = urlparse(self.path).path.rstrip("/")
        toolsets = {v["toolset"]: k for k, v in POLICY["servers"].items()}
        ts = path.rsplit("/", 1)[-1]
        if not path.startswith("/mcp/") or ts not in toolsets:
            return self.send(404, {"detail": "unknown toolset"})
        msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        method = msg.get("method")
        if method == "initialize":
            sid = uuid.uuid4().hex
            self.sessions.add(sid)
            return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "aap-mcp-server-mock", "version": "0"}}}, {"Mcp-Session-Id": sid})
        if self.headers.get("Mcp-Session-Id") not in self.sessions:
            return self.send(400, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "no session"}})
        if method == "notifications/initialized":
            return self.send(202, b"", {"Content-Length": "0"})
        if method == "tools/list":
            server = toolsets[ts]
            names = list(POLICY["read_tools"].get(server, []))
            if self.scenario != "no_write":
                names += POLICY["launch_tools"].get(server, [])
            if server == "aap-jobs":
                names += EXTRA_TOOLS
            tools = [{"name": n, "description": n, "inputSchema": {"type": "object"}} for n in names]
            return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}})
        self.send(200, {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32601, "message": "method"}})

    def reply(self, obj, extra=None):
        if self.scenario == "sse":
            body = f"event: message\ndata: {json.dumps(obj)}\n\n".encode()
            h = {"Content-Type": "text/event-stream"}
        else:
            body, h = json.dumps(obj).encode(), {"Content-Type": "application/json"}
        h.update(extra or {})
        self.send(200, body, h)


def serve(port, scenario="ok"):
    handler = type("H", (Handler,), {"scenario": scenario, "sessions": set()})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


if __name__ == "__main__":
    srv = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8097, sys.argv[2] if len(sys.argv) > 2 else "ok")
    print(f"mock AAP + MCP on http://127.0.0.1:{srv.server_address[1]}", flush=True)
    srv.serve_forever()

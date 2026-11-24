#!/usr/bin/env python3
"""Tests for the editor-agent approval gate (tools/aap_mcp_gate.py) and preflight (tools/aap_mcp_sync.py).

Runs the gate the way the editors do (JSON on stdin, decision on stdout) for Cursor and VS Code / Copilot
payloads, and runs the sync script against tests/mock_aap_mcp.py in each RBAC scenario.

Usage: python3 tests/test_agent_gate.py [-v]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "tools" / "aap_mcp_gate.py"
SYNC = ROOT / "tools" / "aap_mcp_sync.py"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import mock_aap_mcp  # noqa: E402
from aap_mcp_gate import Gate  # noqa: E402

POLICY = json.loads((ROOT / "agent" / "aap_mcp_policy.json").read_text())
CHG = "CHG0001001"


def first(d, pred):
    return next(k for k, v in d.items() if pred(k, v))


RO_HOST = first(POLICY["job_templates"], lambda k, v: v["kind"] == "read-only" and v["requires_limit"]
                and not v["required_vars"])
RO_LOCAL = first(POLICY["job_templates"], lambda k, v: v["kind"] == "read-only" and not v["requires_limit"])
SN_WRITE = first(POLICY["job_templates"], lambda k, v: v["kind"] == "servicenow-write")
WF_HOST = first(POLICY["workflows"], lambda k, v: v["requires_limit"])
WF_LOCAL = first(POLICY["workflows"], lambda k, v: not v["requires_limit"])
BLOCKED = POLICY["blocked_templates"][0]


class Root:
    """Temporary repo root with the policy and a synced ID map."""

    def __init__(self, id_map=True, age_days=0):
        self.dir = Path(tempfile.mkdtemp(prefix="aapgate-"))
        (self.dir / "agent").mkdir()
        shutil.copy(ROOT / "agent" / "aap_mcp_policy.json", self.dir / "agent")
        self.jt = {name: 100 + i for i, name in enumerate(POLICY["job_templates"])}
        self.wf = {name: 500 + i for i, name in enumerate(POLICY["workflows"])}
        self.map = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_days * 86400)),
            "job_templates": {str(v): k for k, v in self.jt.items()},
            "workflow_job_templates": {str(v): k for k, v in self.wf.items()},
        }
        if id_map:
            (self.dir / ".aap-mcp").mkdir()
            (self.dir / ".aap-mcp" / "template_ids.json").write_text(json.dumps(self.map))

    def run(self, client, payload, raw=None):
        env = dict(os.environ, AAP_MCP_GATE_ROOT=str(self.dir))
        p = subprocess.run([sys.executable, str(GATE), "--client", client], env=env, capture_output=True, text=True,
                           input=raw if raw is not None else json.dumps(payload), timeout=30)
        return p.returncode, json.loads(p.stdout)

    def audit(self):
        f = self.dir / ".aap-mcp" / "audit.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def body(limit=None, **ev):
    rb = {}
    if limit is not None:
        rb["limit"] = limit
    if ev:
        rb["extra_vars"] = ev
    return rb


class GateCursor(unittest.TestCase):
    def setUp(self):
        self.r = Root()

    def tearDown(self):
        self.r.cleanup()

    def mcp(self, tool, args=None, server="aap-jobs", url=None):
        url = url or f"https://aap.example.com:8448/mcp/{POLICY['servers'].get(server, {}).get('toolset', 'x')}"
        rc, out = self.r.run("cursor-mcp", {"hook_event_name": "beforeMCPExecution", "tool_name": tool,
                                            "tool_input": json.dumps(args or {}), "mcp_server_name": server,
                                            "url": url})
        self.assertEqual(rc, 0)
        return out["permission"], out.get("agent_message", "")

    def jt(self, name, rb):
        return self.mcp("job_templates_launch_create", {"id": self.r.jt[name], "requestBody": rb})

    def wf(self, name, rb):
        return self.mcp("workflow_job_templates_launch_create", {"id": self.r.wf[name], "requestBody": rb})

    def test_read_tools_allowed(self):
        for server, tools in POLICY["read_tools"].items():
            for t in tools:
                self.assertEqual(self.mcp(t, {}, server)[0], "allow", t)

    def test_denied_and_unlisted_tools(self):
        d, msg = self.mcp("jobs_cancel_create", {"id": 1})
        self.assertEqual(d, "deny")
        self.assertIn("WF", msg)
        self.assertEqual(self.mcp("job_templates_create", {})[0], "deny")
        d, msg = self.mcp("users_list", {})
        self.assertEqual(d, "deny")
        self.assertIn("allowlist", msg)

    def test_discover_endpoint_denied(self):
        d, _ = self.mcp("call_tool", {"name": "job_templates_launch_create"}, server="aap-discover",
                        url="https://aap.example.com:8448/mcp/discover")
        self.assertEqual(d, "deny")
        d, msg = self.mcp("jobs_list", {}, server="my-aap", url="https://aap.example.com:8448/mcp/job_management")
        self.assertEqual(d, "deny")
        self.assertIn("not one of the gated servers", msg)

    def test_read_only_launch(self):
        self.assertEqual(self.jt(RO_HOST, body("web01.example.com"))[0], "allow")
        self.assertEqual(self.jt(RO_LOCAL, body())[0], "allow")
        sv = POLICY["job_templates"][RO_HOST]["survey_vars"]
        if sv:
            self.assertEqual(self.jt(RO_HOST, body("web01", **{sv[0]: "80"}))[0], "allow")
        self.assertEqual(self.mcp("job_templates_launch_create", {
            "id": self.r.jt[RO_HOST], "requestBody": json.dumps(body("web01"))})[0], "allow")

    def test_limit_rules(self):
        for bad in (None, "", "all", "*", "web01,all", "!web01", "&prod"):
            d, msg = self.jt(RO_HOST, body(bad) if bad is not None else body())
            self.assertEqual(d, "deny", bad)
        d, msg = self.jt(RO_LOCAL, body("web01"))
        self.assertEqual(d, "deny")
        self.assertIn("localhost", msg)

    def test_launch_overrides_and_survey(self):
        d, msg = self.jt(RO_HOST, dict(body("web01"), inventory=1))
        self.assertEqual(d, "deny")
        self.assertIn("overrides", msg)
        d, msg = self.jt(RO_HOST, body("web01", ansible_become_password="x"))
        self.assertEqual(d, "deny")
        self.assertIn("does not ask for", msg)
        d, _ = self.mcp("job_templates_launch_create", {"id": self.r.jt[RO_HOST], "requestBody": body("web01"),
                                                        "job_type": "run"})
        self.assertEqual(d, "deny")
        d, _ = self.mcp("job_templates_launch_create", {"id": self.r.jt[RO_HOST],
                                                        "requestBody": {"limit": "web01", "extra_vars": "a: 1"}})
        self.assertEqual(d, "deny")

    def test_unknown_and_blocked_ids(self):
        d, msg = self.mcp("job_templates_launch_create", {"id": 99999, "requestBody": {}})
        self.assertEqual(d, "deny")
        self.assertIn("not an agent-launchable", msg)
        # a changing template's ID never appears in the map; if someone adds it, the name still blocks it
        m = self.r.map
        m["job_templates"]["4242"] = BLOCKED
        (self.r.dir / ".aap-mcp" / "template_ids.json").write_text(json.dumps(m))
        d, msg = self.mcp("job_templates_launch_create", {"id": 4242, "requestBody": body("web01")})
        self.assertEqual(d, "deny")
        self.assertIn("WF - ", msg)

    def test_servicenow_write_asks(self):
        spec = POLICY["job_templates"][SN_WRITE]
        ev = {v: "x" for v in spec["required_vars"]}
        d, msg = self.jt(SN_WRITE, body("web01" if spec["requires_limit"] else None, **ev))
        self.assertEqual(d, "ask")
        self.assertIn("ServiceNow", msg)

    def test_workflow_rules(self):
        spec = POLICY["workflows"][WF_HOST]
        req = {v: "x" for v in spec["required_vars"] if v != "sn_change_number"}
        d, msg = self.wf(WF_HOST, body("web01", **req))
        self.assertEqual(d, "deny")
        self.assertIn("SN - Create change request", msg)
        self.assertEqual(self.wf(WF_HOST, body("web01", sn_change_number="CHG12", **req))[0], "deny")
        self.assertEqual(self.wf(WF_HOST, body("all", sn_change_number=CHG, **req))[0], "deny")
        d, msg = self.wf(WF_HOST, body("web01", sn_change_number=CHG, **req))
        self.assertEqual(d, "ask")
        self.assertIn(POLICY["approver_team"], msg)
        self.assertIn(CHG, msg)
        lspec = POLICY["workflows"][WF_LOCAL]
        lreq = {v: "x" for v in lspec["required_vars"]}
        lreq["sn_change_number"] = CHG
        self.assertEqual(self.wf(WF_LOCAL, body(**lreq))[0], "ask")
        # a workflow ID passed to the job template tool is not in that table
        self.assertEqual(self.mcp("job_templates_launch_create", {"id": self.r.wf[WF_HOST],
                                                                  "requestBody": {}})[0], "deny")

    def test_other_servers_passthrough(self):
        d, msg = self.mcp("search_issues", {}, server="github", url="https://api.githubcopilot.com/mcp/")
        self.assertEqual(d, "ask")
        env_root = self.r.dir
        env = dict(os.environ, AAP_MCP_GATE_ROOT=str(env_root), AAP_MCP_GATE_PASSTHROUGH="allow")
        p = subprocess.run([sys.executable, str(GATE), "--client", "cursor-mcp"], env=env, text=True,
                           capture_output=True, input=json.dumps({"tool_name": "x", "tool_input": "{}",
                                                                  "mcp_server_name": "github", "url": "https://x/"}))
        self.assertEqual(json.loads(p.stdout)["permission"], "allow")

    def test_fail_closed(self):
        rc, out = self.r.run("cursor-mcp", None, raw="not json")
        self.assertEqual((rc, out["permission"]), (2, "deny"))
        rc, out = self.r.run("cursor-mcp", None, raw="[]")
        self.assertEqual(out["permission"], "deny")
        (self.r.dir / "agent" / "aap_mcp_policy.json").unlink()
        rc, out = self.r.run("cursor-mcp", {"tool_name": "jobs_list", "mcp_server_name": "aap-jobs"})
        self.assertEqual((rc, out["permission"]), (2, "deny"))

    def test_audit_log(self):
        self.jt(RO_HOST, body("web01"))
        self.mcp("jobs_cancel_create", {"id": 1})
        recs = self.r.audit()
        self.assertEqual([r["decision"] for r in recs], ["allow", "deny"])
        self.assertEqual(recs[0]["template"], RO_HOST)
        self.assertNotIn("extra_vars", json.dumps(recs))


class GateNoMap(unittest.TestCase):
    def test_launch_denied_reads_allowed_without_map(self):
        r = Root(id_map=False)
        try:
            _, out = r.run("cursor-mcp", {"tool_name": "job_templates_launch_create", "mcp_server_name": "aap-jobs",
                                          "tool_input": json.dumps({"id": 100, "requestBody": {}})})
            self.assertEqual(out["permission"], "deny")
            self.assertIn("aap_mcp_sync.py", out["agent_message"])
            _, out = r.run("cursor-mcp", {"tool_name": "jobs_list", "mcp_server_name": "aap-jobs", "tool_input": "{}"})
            self.assertEqual(out["permission"], "allow")
        finally:
            r.cleanup()

    def test_stale_map_asks(self):
        r = Root(age_days=30)
        try:
            _, out = r.run("cursor-mcp", {"tool_name": "job_templates_launch_create", "mcp_server_name": "aap-jobs",
                                          "tool_input": json.dumps({"id": r.jt[RO_HOST],
                                                                    "requestBody": {"limit": "web01"}})})
            self.assertEqual(out["permission"], "ask")
        finally:
            r.cleanup()


class GateCopilot(unittest.TestCase):
    def setUp(self):
        self.r = Root()

    def tearDown(self):
        self.r.cleanup()

    def run_cp(self, tool, args, snake=False):
        payload = {"tool_name": tool, "tool_input": args} if snake else \
            {"toolName": tool, "toolArgs": json.dumps(args) if isinstance(args, dict) else args}
        rc, out = self.r.run("copilot", payload)
        self.assertEqual(rc, 0)
        return out.get("permissionDecision"), out.get("permissionDecisionReason", "")

    def test_tool_name_forms(self):
        for name in ("jobs_list", "aap-jobs/jobs_list", "mcp_aap-jobs_jobs_list", "mcp__aap-jobs__jobs_list",
                     "aap_jobs_jobs_list", "controller.jobs_list"):
            self.assertEqual(self.run_cp(name, {})[0], "allow", name)
        self.assertEqual(self.run_cp("aap-jobs/workflow_jobs_list", {})[0], "allow")
        self.assertEqual(self.run_cp("aap-jobs/foo_jobs_list", {})[0], "deny")
        self.assertEqual(self.run_cp("aap-jobs/call_tool", {})[0], "deny")

    def test_payload_shapes(self):
        args = {"id": self.r.jt[RO_HOST], "requestBody": {"limit": "web01"}}
        self.assertEqual(self.run_cp("aap-jobs/job_templates_launch_create", args)[0], "allow")
        self.assertEqual(self.run_cp("aap-jobs/job_templates_launch_create", args, snake=True)[0], "allow")
        wf = POLICY["workflows"][WF_HOST]
        ev = {v: "x" for v in wf["required_vars"]}
        ev["sn_change_number"] = CHG
        d, msg = self.run_cp("aap-jobs/workflow_job_templates_launch_create",
                             {"id": self.r.wf[WF_HOST], "requestBody": {"limit": "web01", "extra_vars": ev}})
        self.assertEqual(d, "ask")

    def test_non_aap_tools_no_decision(self):
        rc, out = self.r.run("copilot", {"toolName": "read_file", "toolArgs": json.dumps({"path": "README.md"})})
        self.assertEqual(out, {})
        rc, out = self.r.run("copilot", {"toolName": "github/search_issues", "toolArgs": "{}"})
        self.assertEqual(out, {})

    def test_shell(self):
        deny = ['curl -H "Authorization: Bearer $AAP_MCP_TOKEN" https://aap/api/controller/v2/jobs/',
                "curl -sk https://aap.example.com/api/controller/v2/job_templates/12/launch/ -X POST",
                "awx job_templates launch 12", "ansible-playbook aap_config/configure_aap.yml",
                "python3 -c 'import urllib.request; urllib.request.urlopen(\"https://aap:8448/mcp/job_management\")'",
                "echo $CONTROLLER_OAUTH_TOKEN"]
        for cmd in deny:
            self.assertEqual(self.run_cp("run_in_terminal", {"command": cmd})[0], "deny", cmd)
        for cmd in ("rg /api/controller/ docs", "ls -la", "python3 tests/validate_config.py",
                    "curl https://example.com/api/v1/things"):
            rc, out = self.r.run("copilot", {"toolName": "bash", "toolArgs": json.dumps({"command": cmd})})
            self.assertEqual(out, {}, cmd)

    def test_cursor_shell(self):
        rc, out = self.r.run("cursor-shell", {"command": "awx jobs list", "cwd": "/repo"})
        self.assertEqual(out["permission"], "deny")
        rc, out = self.r.run("cursor-shell", {"command": "grep AAP_ docs/*.md", "cwd": "/repo"})
        self.assertEqual(out["permission"], "allow")

    def test_fail_closed(self):
        rc, out = self.r.run("copilot", None, raw="{")
        self.assertEqual(out["permissionDecision"], "deny")
        rc, out = self.r.run("copilot", {"toolName": "aap-jobs/job_templates_launch_create", "toolArgs": "{bad"})
        self.assertEqual(out["permissionDecision"], "deny")


class Sync(unittest.TestCase):
    def start(self, scenario):
        srv = mock_aap_mcp.serve(0, scenario)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        root = Root(id_map=False)
        self.addCleanup(root.cleanup)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        env = dict(os.environ, AAP_MCP_GATE_ROOT=str(root.dir), AAP_URL=url, AAP_MCP_URL=url,
                   AAP_MCP_TOKEN="agent-token")
        p = subprocess.run([sys.executable, str(SYNC)], env=env, capture_output=True, text=True, timeout=60)
        f = root.dir / ".aap-mcp" / "template_ids.json"
        return p.returncode, p.stdout, (json.loads(f.read_text()) if f.exists() else None)

    def test_ok(self):
        for scenario in ("ok", "sse"):
            rc, out, m = self.start(scenario)
            self.assertEqual(rc, 0, out)
            self.assertEqual(len(m["job_templates"]), len(POLICY["job_templates"]))
            self.assertEqual(len(m["workflow_job_templates"]), len(POLICY["workflows"]))
            self.assertFalse(set(m["job_templates"].values()) & set(POLICY["blocked_templates"]))
            self.assertIn("launch tools exposed", out)

    def test_failures_block_map(self):
        for scenario, text in (("superuser", "superuser"), ("approver", "approve its own"),
                               ("bad_rbac", "can launch 'MON - Disk cleanup'"), ("no_approval", "no approval node")):
            rc, out, m = self.start(scenario)
            self.assertEqual(rc, 1, scenario)
            self.assertIn(text, out, scenario)
            self.assertIsNone(m, scenario)

    def test_write_disabled_warns(self):
        rc, out, m = self.start("no_write")
        self.assertEqual(rc, 0)
        self.assertIn("ALLOW_WRITE_OPERATIONS=true", out)

    def test_bad_token(self):
        srv = mock_aap_mcp.serve(0, "ok")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        root = Root(id_map=False)
        self.addCleanup(root.cleanup)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        env = dict(os.environ, AAP_MCP_GATE_ROOT=str(root.dir), AAP_URL=url, AAP_MCP_URL=url, AAP_MCP_TOKEN="nope")
        p = subprocess.run([sys.executable, str(SYNC)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 1)
        self.assertIn("401", p.stdout)


try:
    import yaml  # noqa: F401  (the generator needs PyYAML; the gate and sync do not)
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


class Generated(unittest.TestCase):
    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_generated_files_current(self):
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_agent_config.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_policy_consistency(self):
        g = Gate(POLICY)
        self.assertFalse(set(POLICY["job_templates"]) & set(POLICY["blocked_templates"]))
        for wf, spec in POLICY["workflows"].items():
            self.assertIn(spec["template"], POLICY["blocked_templates"], wf)
            self.assertEqual(spec["required_vars"][0], "sn_change_number", wf)
        self.assertFalse(g.read & set(POLICY["deny_reasons"]))
        vs = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]
        for server, spec in vs.items():
            self.assertEqual(set(spec["tools"]),
                             set(POLICY["read_tools"][server]) | set(POLICY["launch_tools"].get(server, [])))
            self.assertTrue(spec["url"].endswith("/mcp/" + POLICY["servers"][server]["toolset"]))

    def test_hook_configs(self):
        cur = json.loads((ROOT / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for hs in cur["hooks"].values() for h in hs]
        self.assertIn("python3 tools/aap_mcp_gate.py --client cursor-mcp", cmds)
        self.assertTrue(all(h.get("failClosed") for h in cur["hooks"]["beforeMCPExecution"]))
        cp = json.loads((ROOT / ".github" / "hooks" / "aap-mcp-gate.json").read_text())
        self.assertEqual(cp["hooks"]["preToolUse"][0]["command"], "python3 tools/aap_mcp_gate.py --client copilot")


if __name__ == "__main__":
    unittest.main()

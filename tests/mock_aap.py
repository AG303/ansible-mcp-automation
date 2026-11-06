#!/usr/bin/env python3
"""Minimal AAP 2.6 gateway/controller API mock for functional tests of the API roles.

Every list endpoint returns the same superset objects so Jinja paths in the roles
are exercised without a live platform. Usage: python3 tests/mock_aap.py [port]
Then: AAP_HOSTNAME=http://127.0.0.1:<port> AAP_TOKEN=test ansible-playbook ...
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NOW = datetime.now(timezone.utc)


def ts(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def obj(i, path):
    kind = path.rstrip("/").split("/")[-1]
    failed = i % 2 == 1
    return {
        "id": i, "type": kind.rstrip("s"), "url": f"{path.rstrip('/')}/{i}/",
        "name": ["AAP Ops - Protected sample", "MON - Host health check", "PATCH - Apply updates", "Demo Job Template"][i % 4],
        "description": "mock object", "status": ["successful", "failed", "running", "pending"][i % 4],
        "failed": failed, "started": ts(30 - i), "finished": None if i % 4 in (2, 3) else ts(29 - i),
        "elapsed": 3600.0 * i, "created": ts(24 * 120 + i), "modified": ts(24 * 100 + i), "last_login": ts(24 * 200),
        "launch_type": "manual", "limit": "web01,web02", "job_template": 7, "unified_job_template": 7,
        "job_explanation": "", "result_traceback": "", "canceled_on": None,
        "host_name": f"host{i % 3}.example.com", "host": i, "failures": i % 3, "dark": i % 2, "ok": 5, "changed": 1,
        "enabled": i % 2 == 0, "next_run": ts(-24), "rrule": "DTSTART:20260101T000000Z RRULE:FREQ=DAILY",
        "dtend": None, "timezone": "UTC", "until": "",
        "capacity": 100, "consumed_capacity": 40 + i, "percent_capacity_remaining": 60.0 - i, "jobs_running": i,
        "jobs_total": 50, "instances": 2, "is_container_group": i % 2 == 0, "policy_instance_minimum": 0,
        "node_type": "execution", "node_state": "ready", "health_check_pending": False, "errors": "",
        "last_health_check": ts(1), "hostname": f"exec{i}.example.com", "mem_capacity": 100, "cpu_capacity": 80,
        "image": f"registry.example.com/aap-ops-ee:{i}", "pull": "missing", "managed": False,
        "credential_type": 1, "kind": "ssh", "inputs": {"username": "svc", "password": "$encrypted$"},
        "operation": "update", "object1": "job_template", "object2": "", "timestamp": ts(i),
        "changes": {"name": ["a", "b"]}, "actor": "admin",
        "username": f"user{i}", "is_superuser": i == 1, "is_platform_auditor": False, "email": "u@example.com",
        "expires": ts(-24 * (i * 40 - 30)), "scope": "write", "application": None, "user": i,
        "role_definition": 1, "object_id": "1", "content_type": "shared.organization",
        "authorization_grant_type": "password", "client_type": "confidential", "redirect_uris": "",
        "task": "Gather facts", "event": "runner_on_failed", "event_data": {"res": {"msg": "mock failure"}},
        "stdout": "fatal: mock", "counter": i, "playbook": "playbooks/monitoring/mon_health_check.yml",
        "ask_limit_on_launch": True, "ask_variables_on_launch": True, "survey_enabled": True,
        "execution_environment": 1, "inventory": 1, "project": 1, "organization": 1, "job_type": "run",
        "verbosity": 0, "forks": 0, "timeout": 0, "allow_simultaneous": False, "webhook_service": "",
        "approval_expiration": ts(-2), "timed_out": False, "workflow_job": 1,
        "first_automation": ts(24 * 300), "last_automation": ts(1), "automated_counter": 40 + i, "deleted": False,
        "deleted_counter": 0, "used_in_inventories": 1,
        "last_job_run": ts(24 * 100 * (i % 2)), "last_job_failed": failed, "job": 9 + i, "last_seen": ts(0.1 * i),
        "credential": None if i % 2 else 3, "next_job_run": None, "workflow_job_template": 3,
        "summary_fields": {
            "job_template": {"id": 7, "name": "MON - Host health check"},
            "unified_job_template": {"id": 7, "name": "MON - Host health check", "unified_job_type": "job"},
            "inventory": {"id": 1, "name": "AAP Ops - Managed Hosts"},
            "project": {"id": 1, "name": "AAP Ops"}, "organization": {"id": 1, "name": "Default"},
            "execution_environment": {"id": 1, "name": "AAP Ops EE", "image": "registry.example.com/aap-ops-ee:1"},
            "credentials": [{"id": 1, "name": "AAP Ops - Machine (Linux)", "kind": "ssh"}],
            "credential_type": {"id": 1, "name": "Machine"}, "created_by": {"id": 1, "username": "admin"},
            "modified_by": {"id": 1, "username": "admin"}, "user": {"id": i, "username": f"user{i}"},
            "role_definition": {"id": 1, "name": "Organization Admin"}, "content_object": {"id": 1, "name": "Default"},
            "owner": [{"id": 1, "name": "Default", "type": "organization"}],
            "workflow_job": {"id": 1, "name": "WF - PATCH - Apply updates"},
            "source_workflow_job": {"id": 1, "name": "WF - PATCH - Apply updates", "status": "pending"},
            "job": {"id": 9, "name": "MON - Host health check", "status": "failed"},
            "host": {"id": i, "name": f"host{i % 3}.example.com"}, "actor": {"id": 1, "username": "admin"},
            "instance_group": {"id": 1, "name": "default"}, "last_job": {"id": 9, "status": "failed", "finished": ts(2)},
        },
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urlparse(self.path).path
        if p.endswith("/settings/all/") or p.endswith("/config/") or p.endswith("/ping/"):
            return self._send(200, {"version": "4.7.0", "license_info": {"instance_count": 500, "current_instances_consumed": 120,
                                    "time_remaining": 86400 * 90, "subscription_name": "Mock AAP", "compliant": True},
                                    "ACTIVITY_STREAM_ENABLED": True, "AWX_ISOLATION_SHOW_PATHS": [], "TOWER_URL_BASE": "https://aap.example.com",
                                    "instances": [], "instance_groups": [], "ha": True, "active_node": "aap-controller"})
        m = re.search(r"/(\d+)/$", p)
        if m and not re.search(r"/\d+/[a-z_]+/$", p):
            o = obj(int(m.group(1)), p[: m.start() + 1])
            if "/workflow_jobs/" in p:
                o.update({"limit": "host0.example.com", "status": "running", "name": "WF - PATCH - Apply updates",
                          "extra_vars": json.dumps({"sn_change_number": "CHG0001"})})
            if int(m.group(1)) >= 1000:  # objects created by POST /launch/ finish immediately
                o.update({"status": "successful", "failed": False, "finished": ts(0)})
            return self._send(200, o)
        if p.endswith("/survey_spec/"):
            return self._send(200, {"name": "", "description": "", "spec": []})
        results = [obj(i, p) for i in range(1, 5)]
        q = parse_qs(urlparse(self.path).query)
        if "name" in q:  # exact-name filters return a single matching object
            results = [dict(results[0], name=q["name"][0])]
        if "id" in q:  # id filter (unified_jobs?id=N) returns that object
            results = [obj(int(q["id"][0]), p)]
        return self._send(200, {"count": len(results), "next": None, "previous": None, "results": results})

    def _write(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        out = obj(1001, urlparse(self.path).path)
        out.update({"job": 1001, "system_job": 1002, "status": "pending"})
        out.update(body if isinstance(body, dict) else {})
        return out

    def do_POST(self):
        p = urlparse(self.path).path
        self._send(202 if p.endswith("/cancel/") else 201, self._write())

    def do_PATCH(self):
        self._send(200, self._write())

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()

#!/usr/bin/env python3
"""In-memory ServiceNow Table API mock for functional tests of the sn_* roles.

Supports GET list/single (sysparm_query with ^, ^OR-free =, !=, IN, NOT IN, LIKE,
STARTSWITH, ISEMPTY, ISNOTEMPTY, >, >=, <, <=; ORDERBY ignored), sysparm_display_value
true|false|all, sysparm_limit/offset with X-Total-Count, POST and PATCH.
Usage: python3 tests/mock_servicenow.py [port]; SN_HOST=http://127.0.0.1:<port>
"""
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NOW = datetime.now(timezone.utc)
FMT = "%Y-%m-%d %H:%M:%S"


def sn_ts(hours):
    return (NOW + timedelta(hours=hours)).strftime(FMT)


PREFIX = {"incident": "INC", "change_request": "CHG", "problem": "PRB", "problem_task": "PTASK", "change_task": "CTASK"}
COUNTER = {k: 1000 for k in PREFIX}
DB = {t: [] for t in ["incident", "change_request", "problem", "problem_task", "task_ci", "cmdb_ci",
                      "cmdb_ci_server", "cmn_schedule_span", "sys_properties", "sys_user", "sys_user_group", "change_task"]}
REFS = {}  # sys_id -> display name for reference fields


def add(table, **f):
    rec = {"sys_id": uuid.uuid4().hex, "sys_created_on": sn_ts(-48), "sys_updated_on": sn_ts(-1), "sys_class_name": table}
    if table in PREFIX:
        rec["active"] = "true"
        COUNTER[table] += 1
        rec["number"] = f"{PREFIX[table]}{COUNTER[table]:07d}"
    rec.update(f)
    DB.setdefault(table, []).append(rec)
    if table.startswith("cmdb_ci_") and table != "cmdb_ci_server":  # class tables extend cmdb_ci_server
        DB["cmdb_ci_server"].append(dict(rec, sys_class_name=table))
    if "name" in rec:
        REFS[rec["sys_id"]] = rec["name"]
    if "number" in rec:
        REFS[rec["sys_id"]] = rec["number"]
    return rec


def seed():
    ci = add("cmdb_ci_server", name="localhost", fqdn="localhost", ip_address="127.0.0.1", os="Ubuntu", sys_class_name="cmdb_ci_linux_server")
    DB["cmdb_ci"].append(dict(ci))
    ci2 = add("cmdb_ci_server", name="web01.example.com", fqdn="web01.example.com", ip_address="10.0.0.11", os="Red Hat Enterprise Linux")
    DB["cmdb_ci"].append(dict(ci2))
    add("sys_properties", name="glide.buildtag", value="glide-xanadu-07-02-2024")
    for i in range(4):
        add("incident", short_description=f"MON - Host health check failed on localhost ({i})", state="2" if i else "1",
            impact="2", urgency="2", priority=str(3 + (i % 2)), cmdb_ci=ci["sys_id"], assignment_group="",
            correlation_id="aap_ops:MON - Host health check:localhost", correlation_display="Ansible Automation Platform",
            problem_id="", opened_at=sn_ts(-24 * (i + 1)), category="software", caller_id="", work_notes="", close_code="")
    add("change_request", short_description="WF - PATCH - Apply updates", type="normal", state="-2", approval="approved",
        on_hold="false", start_date=sn_ts(-1), end_date=sn_ts(3), risk="3", assignment_group="",
        correlation_id="aap_ops:change:PATCH - Apply updates", description="Template: PATCH - Apply updates",
        u_aap_template="PATCH - Apply updates", work_start="", work_end="", close_code="", close_notes="")
    chg = DB["change_request"][0]
    add("task_ci", task=chg["sys_id"], ci_item=ci["sys_id"])
    add("change_request", short_description="Overlapping maintenance", type="normal", state="-2", approval="approved",
        on_hold="false", start_date=sn_ts(0), end_date=sn_ts(2), risk="3", correlation_id="")
    add("task_ci", task=DB["change_request"][1]["sys_id"], ci_item=ci["sys_id"])
    add("problem", short_description="Recurring health check failures on localhost", state="101", impact="2", urgency="2",
        cmdb_ci=ci["sys_id"], cause_notes="", fix_notes="", resolution_code="", assigned_to="")


def display(table, rec, mode):
    if mode in ("false", "", None):
        return rec
    out = {}
    for k, v in rec.items():
        d = REFS.get(v, v) if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v or "") and k != "sys_id" else v
        out[k] = d if mode == "true" else {"value": v, "display_value": d}
    return out


OPS = re.compile(r"^([A-Za-z_0-9.]+?)(NOT IN|ISNOTEMPTY|ISEMPTY|STARTSWITH|NOT LIKE|LIKE|IN|!=|>=|<=|=|>|<)(.*)$")


def match(rec, cond):
    cond = cond.strip()
    if not cond or cond.startswith("ORDERBY"):
        return True
    m = OPS.match(cond)
    if not m:
        return True
    f, op, v = m.groups()
    cur = str(rec.get(f, "")) if "." not in f else ""
    if "javascript:" in v:
        return True
    return {
        "ISEMPTY": lambda: cur == "", "ISNOTEMPTY": lambda: cur != "", "=": lambda: cur == v, "!=": lambda: cur != v,
        "IN": lambda: cur in v.split(","), "NOT IN": lambda: cur not in v.split(","),
        "LIKE": lambda: v.lower() in cur.lower(), "NOT LIKE": lambda: v.lower() not in cur.lower(),
        "STARTSWITH": lambda: cur.startswith(v), ">": lambda: cur > v, ">=": lambda: cur >= v,
        "<": lambda: cur < v, "<=": lambda: cur <= v,
    }[op]()


def query(table, q):
    rows = DB.get(table, [])
    if not q:
        return list(rows)
    groups = [g.split("^") for g in q.split("^NQ")]
    out = []
    for r in rows:
        for conds in groups:
            ok = True
            # "a=1^ORb=2" arrives here as ["a=1", "ORb=2"]; fold OR terms into the previous condition.
            folded = []
            for c in conds:
                if c.startswith("OR") and folded:
                    folded[-1].append(c[2:])
                else:
                    folded.append([c])
            for alts in folded:
                if not any(match(r, a) for a in alts):
                    ok = False
                    break
            if ok:
                out.append(r)
                break
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    def _send(self, code, body, total=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if total is not None:
            self.send_header("X-Total-Count", str(total))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _parts(self):
        u = urlparse(self.path)
        m = re.match(r"^/api/now/table/([a-z_0-9]+)(?:/([0-9a-f]{32}))?/?$", u.path)
        return m, {k: v[0] for k, v in parse_qs(u.query).items()}

    def do_GET(self):
        m, q = self._parts()
        if not m:
            return self._send(404, {"error": {"message": "not found"}})
        table, sys_id = m.group(1), m.group(2)
        if table not in DB:
            return self._send(400, {"error": {"message": f"Invalid table {table}"}})
        mode = q.get("sysparm_display_value", "false")
        if sys_id:
            r = next((x for x in DB[table] if x["sys_id"] == sys_id), None)
            return self._send(200 if r else 404, {"result": display(table, r, mode)} if r else {"error": {"message": "No Record found"}})
        rows = query(table, q.get("sysparm_query", ""))
        off, lim = int(q.get("sysparm_offset", 0)), int(q.get("sysparm_limit", 10000))
        fields = [f for f in q.get("sysparm_fields", "").split(",") if f]
        page = rows[off: off + lim]
        if fields:
            page = [{k: r.get(k, "") for k in fields} for r in page]
        return self._send(200, {"result": [display(table, r, mode) for r in page]}, total=len(rows))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_POST(self):
        m, q = self._parts()
        if m and m.group(1).startswith("cmdb_ci_"):
            DB.setdefault(m.group(1), [])
        if not m or m.group(1) not in DB:
            return self._send(400, {"error": {"message": "bad table"}})
        rec = add(m.group(1), **self._body())
        self._send(201, {"result": display(m.group(1), rec, q.get("sysparm_display_value", "false"))})

    def do_PATCH(self):
        m, q = self._parts()
        if not m or not m.group(2):
            return self._send(400, {"error": {"message": "sys_id required"}})
        r = next((x for x in DB[m.group(1)] if x["sys_id"] == m.group(2)), None)
        if not r:
            return self._send(404, {"error": {"message": "No Record found"}})
        body = self._body()
        notes = body.pop("work_notes", None)
        r.update(body)
        if notes:
            r["work_notes"] = (r.get("work_notes", "") + "\n---\n" + notes).strip()
        self._send(200, {"result": display(m.group(1), r, q.get("sysparm_display_value", "false"))})

    do_PUT = do_PATCH

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()


if __name__ == "__main__":
    seed()
    if "--dump" in sys.argv:
        print(json.dumps({t: [r.get("number", r.get("name", r["sys_id"])) for r in rows] for t, rows in DB.items()}, indent=1))
        sys.exit()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8098
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()

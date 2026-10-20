"""Generate benchmark-only SQL, contracts and deterministic tenant identities."""
import json
import copy
from .common import ROOT


def contract():
    integer = {"type": "integer", "minimum": 1}
    row = {"type": "object", "required": ["id", "version", "payload"], "additionalProperties": False,
           "properties": {"id": integer, "version": integer, "payload": {"type": "string"}}}
    page = {"type": "object", "required": ["rows"], "additionalProperties": False,
            "properties": {"rows": {"type": "array", "maxItems": 100, "items": row}}}
    endpoints = []
    for name, read_only, output in (("bench_one", True, row), ("bench_page", True, page),
                                    ("bench_write", False, row)):
        properties = {"padding": {"type":"string", "maxLength":65536}}
        if name != "bench_page": properties["id"] = integer
        endpoints.append({"name": name, "function": "web." + name, "read_only": read_only,
                          "input": {"type": "object", "additionalProperties": False,
                                    "required": [] if name == "bench_page" else ["id"], "properties": properties},
                          "output": output})
    for original in list(endpoints):
        public = copy.deepcopy(original)
        public['name'] = original['name'].replace('bench_', 'bench_public_')
        public['function'] = 'web.' + public['name']
        public['input']['properties']['tenant'] = {'type': 'string', 'minLength': 1, 'maxLength': 256}
        public['input']['required'].append('tenant')
        endpoints.append(public)
    return {"version": 1, "endpoints": endpoints,
            "streams": [{"name": "bench", "authorize": "web.bench_authorize",
                         "input": {"type": "object", "additionalProperties": False}}]}


def seed_sql(rows, tenants, payload_bytes):
    if rows < tenants * 100 or rows % tenants:
        raise ValueError("rows must be divisible by tenants, with at least 100 rows per tenant")
    if not 1 <= tenants <= 10000 or not 1 <= payload_bytes <= 8192:
        raise ValueError("tenants must be 1..10000 and payload bytes 1..8192")
    return f"""
BEGIN;
TRUNCATE pgxbench.rows, pgxbench.accounts;
INSERT INTO pgxbench.accounts(subject, route)
SELECT lpad(to_hex(i),32,'0')::uuid::text, lpad(to_hex(1000000+i),32,'0')::uuid::text
FROM generate_series(1,{tenants}) i;
INSERT INTO pgxbench.rows(subject,id,payload)
SELECT a.subject, i, repeat('x',{payload_bytes}) FROM pgxbench.accounts a
CROSS JOIN generate_series(1,{rows // tenants}) i;
COMMIT;
ANALYZE pgxbench.accounts;
ANALYZE pgxbench.rows;
"""


def initialization(password):
    # Generated passwords are hexadecimal. Do not interpolate operator-provided SQL.
    if not password or any(c not in "0123456789abcdef" for c in password):
        raise ValueError("expected generated hexadecimal password")
    return (ROOT / "sql" / "init.sql").read_text().replace("WEB_PASSWORD_PLACEHOLDER", password)

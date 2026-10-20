"""Operator-side database observations and isolated-container network delay."""
import ipaddress
import json
from pathlib import Path
import platform
import time
from urllib.parse import urlsplit, parse_qsl

from .common import command, dump, load_settings, public_settings
from .resources import discover
from .setup import sql


def stats(workdir, output):
    statement = """
SELECT jsonb_build_object(
 'database', (SELECT to_jsonb(d) FROM pg_stat_database d WHERE datname=current_database()),
 'connections', (SELECT coalesce(jsonb_agg(jsonb_build_object('user',usename,'application',application_name,
     'state',state,'wait_event_type',wait_event_type,'wait_event',wait_event,
     'ssl',s.ssl,'ssl_version',s.version,'ssl_cipher',s.cipher)), '[]'::jsonb)
     FROM pg_stat_activity a LEFT JOIN pg_stat_ssl s USING (pid)
     WHERE datname=current_database() AND a.pid<>pg_backend_pid()),
 'functions', (SELECT coalesce(jsonb_agg(to_jsonb(f)), '[]'::jsonb)
     FROM pg_stat_user_functions f WHERE schemaname='web'),
 'settings', jsonb_build_object('server_version',current_setting('server_version'),
     'shared_buffers',current_setting('shared_buffers'),'work_mem',current_setting('work_mem'),
     'max_connections',current_setting('max_connections'),'track_functions',current_setting('track_functions'),
     'ssl',current_setting('ssl')));
"""
    result = {"kind": "database_observation", "timestamp": time.time(),
              "configuration": public_settings(load_settings(workdir)), "postgres": json.loads(sql(workdir, statement)),
              "note": "Operator observation outside the measured interval; observer connection excluded. Function statistics can flush asynchronously."}
    dump(output, result)
    return {"output": str(output), "database_connections": len(result["postgres"]["connections"])}


def network_delay(workdir, delay_ms):
    if platform.system() != "Linux":
        raise RuntimeError("network-delay must run as an operator on the Linux Docker host")
    if not 0 <= delay_ms <= 1000:
        raise ValueError("delay must be 0..1000 milliseconds; 0 clears it")
    settings = load_settings(workdir)
    address, port = "172.29.88.2", 5432
    if settings["external_database"]:
        url = urlsplit(settings["external_database_url"])
        address, port = dict(parse_qsl(url.query))["hostaddr"], url.port or 5432
    parsed = ipaddress.ip_address(address)
    groups = discover(workdir)
    affected = []
    for name, info in groups.items():
        if info["role"] != "api":
            continue
        prefix = ["nsenter", "--target", str(info["pid"]), "--net", "tc"]
        previous = command(prefix + ["qdisc", "show", "dev", "eth0"])
        ours = "qdisc prio 1:" in previous
        if delay_ms:
            if not ours and any("root" in line and "noqueue" not in line for line in previous.splitlines()):
                raise RuntimeError("refusing to replace an existing unrelated qdisc in " + name)
            command(prefix + ["qdisc", "replace", "dev", "eth0", "root", "handle", "1:", "prio", "bands", "3"])
            command(prefix + ["qdisc", "replace", "dev", "eth0", "parent", "1:3", "handle", "30:", "netem", "delay", f"{delay_ms}ms"])
            command(prefix + ["filter", "replace", "dev", "eth0", "protocol", "ip" if parsed.version == 4 else "ipv6",
                "parent", "1:", "prio", "1", "flower", "ip_proto", "tcp", "dst_ip", str(parsed), "dst_port", str(port), "flowid", "1:3"])
        elif ours:
            command(prefix + ["qdisc", "del", "dev", "eth0", "root"])
        affected.append(name)
    if not affected:
        raise RuntimeError("no running benchmark API containers")
    settings["database_egress_delay_ms"] = delay_ms
    dump(Path(workdir) / "settings.json", settings, private=True)
    return {"services": affected, "database_egress_delay_ms": delay_ms,
            "note": "Only DB-destination TCP packets are delayed. Measure resulting RTT. Reapply after container recreation."}

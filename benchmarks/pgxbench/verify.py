"""Short functional verification of the isolated adapters, never a capacity run."""
import asyncio
import json
from pathlib import Path
import time

from .campaign import run_namespace, wait_ready
from .common import compose, dump, load_settings, TARGETS
from .load import Driver, run
from .setup import lock, render, seed, sql


async def verify(args):
    original = load_settings(args.workdir)
    targets = args.targets.split(",")
    if any(target not in TARGETS for target in targets):
        raise ValueError("unknown verification target")
    outcomes = []
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    try:
        for target in targets:
            result = {"target": target, "passed": False}
            try:
                await asyncio.to_thread(compose, args.workdir, "down", "--remove-orphans")
                render(args.workdir, {"target": target, "cpus": 1, "workers": 1, "memory_mib": 0,
                                      "pool_mode": "growing", "cgroup_parent": "", "cgroup_path": "",
                                      "frontend": "tls" if args.url.startswith("https:") else "http",
                                      "auth_mode": "jwt", "auth_fixture": "tenant", "jwt_cache": "default"})
                flags = ["up", "--detach", "--remove-orphans"] + (["--build"] if args.build else [])
                await asyncio.to_thread(compose, args.workdir, *flags)
                await wait_ready(args.workdir, args.url, 120)
                if target == "supabase":
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        ready = await asyncio.to_thread(sql, args.workdir,
                            "SELECT to_regclass('realtime.messages') IS NOT NULL;")
                        if ready.strip() == "t":
                            break
                        await asyncio.sleep(1)
                    else:
                        raise RuntimeError("Realtime tenant migrations did not finish")
                await asyncio.to_thread(seed, args.workdir, 10000, 100, 128, target == "supabase")
                await asyncio.to_thread(lock, args.workdir)
                job = {"target": target, "workload": "page", "rate": 5, "duration": 2, "warmup": 0,
                       "sockets": 0 if target == "postgrest" else 2, "subscriptions": 1,
                       "active_tenants": 2, "probe_rate": 1, "realtime_mode": "both"}
                config = run_namespace(args.workdir, output / (target + ".json"), job, args.url)
                config.engine = "python"
                config.purpose = "functional"
                trial = await run(config)
                counts = trial["results"]["counts"]
                if any(counts.get(name, 0) for name in ("probe_timeout", "missing_notifications", "missing_fresh_snapshots", "invalid_response")):
                    raise RuntimeError("functional delivery/freshness check failed")
                if target not in ("postgrest", "supabase"):
                    await rollback_check(args.workdir, config)
                if args.wrk_image:
                    for engine in ("wrk", "wrk2"):
                        rest = run_namespace(args.workdir, output / (target + "-" + engine + ".json"), {**job, "sockets": 0}, args.url)
                        rest.engine, rest.wrk_image = engine, args.wrk_image
                        rest.purpose = "functional"
                        rest.duration, rest.warmup, rest.rate = (20 if engine == "wrk2" else 1), 0, 5
                        rest.threads, rest.connections = 1, 1
                        measured = await run(rest)
                        if not measured["results"]["counts"].get("http_200"):
                            raise RuntimeError("wrk adapter did not produce successful requests")
                result.update(passed=True, checks=["HTTP/JWT/tenant_preflight", "versioned_realtime" if job["sockets"] else "REST_only",
                                                  "invalid_output_rollback" if target not in ("postgrest", "supabase") else "native_schema_gap"])
                if args.wrk_image:
                    result["checks"].extend(["wrk_status_accounting", "wrk2_corrected_latency"])
            except Exception as error:
                result.update(failure_type=type(error).__name__, detail=str(error) if not hasattr(error, "stderr") else "Docker/SQL command failed; inspect isolated service logs")
                try:
                    logs = await asyncio.to_thread(compose, args.workdir, "logs", "--tail", "40")
                    for value in load_settings(args.workdir).values():
                        if isinstance(value, str) and len(value) >= 32:
                            logs = logs.replace(value, "[redacted]")
                    dump(output / (target + "-diagnostic.json"), {"logs": logs}, private=True)
                except Exception:
                    pass
            outcomes.append(result)
            dump(output / "verification.json", {"kind": "functional_only", "outcomes": outcomes})
    finally:
        await asyncio.to_thread(compose, args.workdir, "down", "--remove-orphans")
        render(args.workdir, original)
    return {"kind": "functional_only", "passed": all(item["passed"] for item in outcomes), "outcomes": outcomes}


async def rollback_check(workdir, config):
    saved = await asyncio.to_thread(sql, workdir, "SELECT pg_get_functiondef('web.bench_write(jsonb)'::regprocedure);")
    driver = Driver(config)
    try:
        _, before = await driver.call("bench_one", {"id": 1}, measure=False)
        await asyncio.to_thread(sql, workdir, """
CREATE OR REPLACE FUNCTION web.bench_write(input jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pgxbench AS $$
BEGIN
  UPDATE pgxbench.rows SET version=version+1
    WHERE subject=current_setting('request.jwt.claims',true)::jsonb->>'sub' AND id=1;
  RETURN '{"invalid":"output must roll back"}'::jsonb;
END $$;
""")
        status, _ = await driver.call("bench_write", {"id": 1}, measure=False)
        _, after = await driver.call("bench_one", {"id": 1}, measure=False)
        if status != "http_503" or before != after:
            raise RuntimeError("invalid output committed a mutation or was not rejected")
    finally:
        await asyncio.to_thread(sql, workdir, saved)
        await driver.close()

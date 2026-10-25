"""Explicit fault/reconnect checks; all mutations target the isolated fixture."""
import asyncio
import json
from pathlib import Path
import time

from .campaign import run_namespace, wait_ready
from .common import compose, dump, load_settings, route, subject
from .load import Driver, Probe
from .setup import sql


async def open_clients(driver, count):
    semaphore = asyncio.Semaphore(8)
    async def open_one(index):
        async with semaphore:
            await driver.socket(index)
    await asyncio.gather(*(open_one(i) for i in range(count)))


async def exercise(args):
    settings = load_settings(args.workdir)
    target = settings["target"]
    if target == "postgrest":
        raise ValueError("standalone PostgREST has no realtime protocol for these exercises")
    if args.sockets < 1 or args.cycles < 1 or args.burst_size < 1:
        raise ValueError("socket/cycle/burst counts must be positive")
    if args.scenario == "listener-loss" and target == "supabase":
        raise ValueError("Supabase Broadcast uses WAL, not pgxio's LISTEN connection; use database-restart")
    job = {"target": target, "workload": "page", "rate": 0, "duration": 1, "warmup": 0,
           "sockets": args.sockets, "subscriptions": 1, "active_tenants": 1,
           "realtime_mode": "both", "probe_rate": 1}
    driver = Driver(run_namespace(args.workdir, args.output, job, args.url))
    report = {"kind": "resilience_exercise", "target": target, "scenario": args.scenario,
              "started_unix": time.time(), "configuration": {
                  "sockets": args.sockets, "cycles": args.cycles, "burst_size": args.burst_size},
              "expected_native_semantics": "cached_channel_authorization" if target == "supabase" else "per_invalidation_reauthorization"}
    restored = False
    try:
        await driver.preflight()
        await open_clients(driver, args.sockets)
        if args.scenario == "reconnect":
            cycles = []
            for _ in range(args.cycles):
                await driver.close()
                driver = Driver(run_namespace(args.workdir, args.output, job, args.url))
                started = time.perf_counter()
                await open_clients(driver, args.sockets)
                await driver.probe_loop(.1)
                cycles.append({"connect_subscribe_snapshot_ms": (time.perf_counter() - started) * 1000,
                               "metrics": driver.metrics.summary(1)})
            report["cycles"] = cycles
            report["passed"] = all(not c["metrics"]["counts"].get("probe_timeout", 0) for c in cycles)
        elif args.scenario == "revocation":
            # Observe the raw protocol so a cached-authorization difference is
            # distinguishable from a reconnect or client-side auth rejection.
            for task in list(driver.tasks):
                task.cancel()
            await asyncio.gather(*list(driver.tasks), return_exceptions=True)
            started = time.perf_counter()
            await asyncio.to_thread(sql, args.workdir,
                f"UPDATE pgxbench.accounts SET enabled=false WHERE subject='{subject(0)}';"
                f"SELECT pg_notify('pgxio_events','{route(0)}');")
            if target == "supabase":
                # Native Broadcast delivery triggers only on table changes.
                await asyncio.to_thread(sql, args.workdir,
                    f"UPDATE pgxbench.rows SET version=version+1 WHERE subject='{subject(0)}' AND id=1;")
            observed = []
            for ws in driver.sockets:
                try:
                    value = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    observed.append(value.get("type") == "changed" or value.get("event") == "broadcast")
                except Exception:
                    observed.append(False)
            report.update(observed_post_revocation_delivery=any(observed),
                          observation_ms=(time.perf_counter() - started) * 1000,
                          passed=not any(observed) if target != "supabase" else None,
                          note="Supabase Broadcast caches authorization; native behavior is reported, not treated as pgxio parity")
        elif args.scenario == "burst":
            expected = {(i, "0") for i in range(args.sockets)}
            probe = Probe(time.perf_counter(), expected, set(range(args.sockets)))
            driver.probes[0] = probe
            semaphore = asyncio.Semaphore(64)
            async def write():
                async with semaphore:
                    return await driver.call("bench_write", {"id": 1})
            results = await asyncio.gather(*(write() for _ in range(args.burst_size)))
            versions = [value["version"] for status, value in results if status == "http_200"]
            probe.version = max(versions, default=0)
            probe.version_ready.set()
            try:
                await asyncio.wait_for(probe.done.wait(), args.timeout)
            except asyncio.TimeoutError:
                pass
            report.update(burst_successes=len(versions), notifications_missing=len(probe.expected),
                          fresh_snapshots_missing=len(probe.fresh_expected),
                          passed=bool(versions) and not probe.expected and not probe.fresh_expected,
                          note="Coalescing is allowed; freshness is verified against the latest acknowledged version")
        else:
            started = time.perf_counter()
            if args.scenario == "listener-loss":
                await asyncio.to_thread(sql, args.workdir,
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='pgxbench' "
                    "AND pid<>pg_backend_pid() AND query='LISTEN pgxio_events';")
            elif args.scenario == "database-restart":
                if settings["external_database"]:
                    raise ValueError("restart the external DB through its operator; this command only restarts the dedicated local postgres service")
                await asyncio.to_thread(compose, args.workdir, "restart", "postgres")
            elif target in ("pgxio", "openresty"):
                binary = "nginx" if target == "pgxio" else "openresty"
                config = "/bench/nginx.conf" if target == "pgxio" else "/bench/openresty.conf"
                await asyncio.to_thread(compose, args.workdir, "exec", "-T", target, binary, "-c", config, "-s", "reload")
            else:
                await asyncio.to_thread(compose, args.workdir, "restart", "realtime" if target == "supabase" else target)
            await driver.close()
            await wait_ready(args.workdir, args.url, args.timeout)
            deadline = time.perf_counter() + args.timeout
            while True:
                driver = Driver(run_namespace(args.workdir, args.output, job, args.url))
                try:
                    await open_clients(driver, args.sockets)
                    await driver.probe_loop(.1)
                    break
                except Exception:
                    await driver.close()
                    if time.perf_counter() >= deadline:
                        raise
                    await asyncio.sleep(.5)
            report.update(recovery_to_fresh_snapshot_ms=(time.perf_counter() - started) * 1000,
                          passed=not driver.metrics.counts.get("probe_timeout", 0)
                                 and driver.metrics.fresh.count == args.sockets)
        report["metrics"] = driver.metrics.summary(max(.001, time.time() - report["started_unix"]))
    except BaseException as error:
        report.update(passed=False, failure_type=type(error).__name__)
        raise
    finally:
        # Always restore the benchmark account, including Ctrl-C during revocation.
        if args.scenario == "revocation":
            try:
                await asyncio.to_thread(sql, args.workdir,
                    f"UPDATE pgxbench.accounts SET enabled=true WHERE subject='{subject(0)}';")
                restored = True
            finally:
                report["account_restored"] = restored
        await driver.close()
        report["finished_unix"] = time.time()
        dump(args.output, report)
    return report

import argparse
import asyncio
import json
from pathlib import Path
import random
import ssl
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .common import TARGETS, compose, dump, load_settings, public_settings
from .load import run
from .resources import apply_limits, collect
from .setup import lock, render, seed


def matrix(args):
    targets = args.targets.split(",")
    if any(t not in TARGETS for t in targets):
        raise ValueError("unknown target in matrix")
    jobs = []
    repetitions = args.repetitions if args.repetitions is not None else (3 if args.kind in ("ram", "host") else (1 if args.kind == "soak" else 5))
    duration = args.duration if args.duration is not None else (3600 if args.kind in ("ram", "host") else (86400 if args.kind == "soak" else 300))
    for target in targets:
        if args.kind == "realtime" and target == "postgrest":
            continue
        if args.kind == "cpu":
            cases = [(cpu, mode, 0, 0, workload, rate, 1)
                for cpu in (1, 2, 4, 8) for mode in ("fixed", "growing")
                for workload in ("one", "page", "write", "mixed") for rate in args.rates]
        elif args.kind in ("ram", "host"):
            cases = [(1, "growing", memory, 0 if target == "postgrest" else 100,
                      "page" if target == "postgrest" else "small-app", 50, 1) for memory in args.memory_values]
        elif args.kind == "soak":
            cases = [(1, "growing", args.memory_values[-1], 0 if target == "postgrest" else 100,
                      "page" if target == "postgrest" else "small-app", 50, 1)]
        else:
            cases = [(1, "growing", 0, sockets, "page", rate, subscriptions)
                     for sockets in (100, 1000, 10000) for rate in sorted(set([0] + args.rates))
                     for subscriptions in ((1,) if target == "supabase" else (1, 16))]
        for cpu, mode, memory, sockets, workload, rate, subscriptions in cases:
            for repetition in range(repetitions):
                jobs.append({"target": target, "cpus": cpu, "workers": cpu, "pool_mode": mode,
                    "memory_mib": 0 if args.kind == "host" else memory,
                    "vm_memory_mib": memory if args.kind == "host" else 0,
                    "max_websockets": max(1024, sockets * 2), "rate": rate,
                    "duration": duration, "warmup": args.warmup, "workload": workload, "sockets": sockets,
                    "subscriptions": subscriptions, "active_tenants": 10, "probe_rate": 1,
                    "realtime_mode": "both", "repetition": repetition + 1})
    auth_modes = getattr(args, 'auth_modes', 'jwt').split(',')
    fixture = getattr(args, 'auth_fixture', 'tenant')
    if any(mode not in ('jwt', 'none') for mode in auth_modes):
        raise ValueError('auth modes must be jwt,none')
    if 'none' in auth_modes and (fixture != 'public' or any(j['sockets'] or j['target'] not in ('pgxio', 'postgrest') for j in jobs)):
        raise ValueError('no-JWT matrices require public REST fixtures and pgxio/postgrest targets')
    if fixture == 'public' and any(j['sockets'] for j in jobs):
        raise ValueError('public authentication-cost fixtures support REST only')
    jobs = [{**job, 'auth_mode': mode, 'auth_fixture': fixture,
             'jwt_cache': getattr(args, 'jwt_cache', 'default')} for job in jobs for mode in auth_modes]
    random.Random(args.seed).shuffle(jobs)
    for index, job in enumerate(jobs):
        job["id"] = f"{index:05d}"
    result = {"schema_version": 1, "kind": args.kind, "seed": args.seed, "jobs": jobs,
              "defaults": {"rows": args.rows, "tenants": 100, "payload_bytes": 128},
              "warning": "Execute on an isolated benchmark host. CPU jobs require separate, fixed database CPU resources."}
    dump(args.output, result)
    return {"jobs": len(jobs), "file": str(args.output),
            "minimum_measured_hours": sum(j["duration"] for j in jobs) / 3600}


async def wait_ready(workdir, url, timeout=120):
    settings = load_settings(workdir)
    ready_url = url.rstrip('/') + '/readyz'
    if settings['target'] == 'postgrest' and settings.get('frontend') == 'http':
        parsed = urlsplit(url)
        host = parsed.hostname
        if ':' in host:
            host = '[' + host + ']'
        ready_url = urlunsplit(('http', f"{host}:{settings.get('admin_port',18081)}", '/ready', '', ''))
    ca = Path(workdir) / "runtime" / "tls-cert.pem"
    verify = ssl.create_default_context(cafile=str(ca)) if url.startswith("https:") else True
    started = time.perf_counter()
    async with httpx.AsyncClient(verify=verify, timeout=2, trust_env=False) as client:
        while time.perf_counter() - started < timeout:
            try:
                response = await client.get(ready_url)
                if response.status_code == 200:
                    return (time.perf_counter() - started) * 1000
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)
    raise RuntimeError("benchmark API did not become ready")


def run_namespace(workdir, output, job, url="http://localhost:18080"):
    return argparse.Namespace(workdir=Path(workdir), output=Path(output), target=job["target"],
        url=url, ws_url=None, ca=None, api_key_env=None, workload=job["workload"], rate=job["rate"],
        duration=job["duration"], warmup=job["warmup"], sockets=job["sockets"],
        subscriptions=job["subscriptions"], active_tenants=job["active_tenants"], realtime_mode=job["realtime_mode"],
        probe_rate=job["probe_rate"], max_inflight=256, timeout=10, event_timeout=5, seed=1,
        slow_reader_fraction=0, slow_reader_delay=1, engine=job.get('engine', "python" if job["sockets"] else "wrk2"),
        threads=job.get('threads', 2), connections=job.get('connections', 16),
        wrk_binary=None, wrk_image=job.get("wrk_image"), wrk_cpuset=job.get('wrk_cpuset'),
        auth_mode=job.get('auth_mode'), auth_fixture=job.get('auth_fixture'), request_bytes=job.get('request_bytes', 0))


async def execute(args):
    specification = json.loads(Path(args.file).read_text())
    jobs = specification["jobs"]
    if not args.execute:
        return {"dry_run": True, "jobs": len(jobs), "file": str(args.file),
                "execution": "add --execute to run; no containers or limits changed"}
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if args.job is not None:
        jobs = [jobs[args.job]]
    outcomes = []
    initial = load_settings(args.workdir)
    available_cpus = expand_cpuset(initial["cpuset"]) if initial.get("cpuset") else []
    for index, job in enumerate(jobs):
        if args.wrk_image:
            job = {**job, "wrk_image": args.wrk_image}
        job_index = job.get("id", f"{args.job if args.job is not None else index:05d}")
        path = output / f"{job_index}-{job['target']}-{job['cpus']}cpu-{job['memory_mib']}m-r{job['repetition']}.json"
        if path.exists():
            raise FileExistsError("result already exists; use a fresh output directory: " + str(path))
        stop = asyncio.Event()
        collector = None
        result = {"job": job, "result": str(path), "started_unix": time.time()}
        try:
            current = load_settings(args.workdir)
            if args.collect and not current.get("cpuset"):
                raise ValueError("campaign publication requires an explicit --cpuset; render first")
            # Stop the previous API and database before changing the generated
            # service graph. Volumes are retained; only the fixture is reseeded.
            await asyncio.to_thread(compose, args.workdir, "down", "--remove-orphans")
            updates = {key: job[key] for key in ("target", "cpus", "workers", "pool_mode", "memory_mib", "max_websockets")}
            updates["vm_memory_mib"] = job.get("vm_memory_mib", 0)
            updates.update({k: job[k] for k in ('auth_mode', 'auth_fixture', 'jwt_cache') if k in job})
            if updates["vm_memory_mib"]:
                from .resources import host_sample
                actual = host_sample()["memory_total_bytes"]
                requested = updates["vm_memory_mib"] * 1048576
                if abs(actual-requested) > max(32*1048576, requested*.1):
                    raise ValueError("provision the requested VM RAM first and select its matching --job; container limits do not resize a VM")
            if current.get("cpuset"):
                if len(available_cpus) < job["cpus"]:
                    raise ValueError("configured --cpuset must contain at least as many CPUs as the largest campaign job")
                updates["cpuset"] = ",".join(map(str, available_cpus[:job["cpus"]]))
            updates.update(specification["defaults"])
            updates["frontend"] = "tls" if args.url.startswith("https:") else "http"
            config = render(args.workdir, updates)
            if config.get("cgroup_path"):
                # A persistent parent/anchor is required for limits BEFORE startup.
                apply_limits(config["cgroup_path"], job["cpus"], job["memory_mib"], config.get("cpuset") or None)
            if args.collect:
                collector = asyncio.create_task(collect(args.workdir, path.with_suffix(".resources.jsonl"), stop=stop))
                await asyncio.sleep(.05)
            started = time.perf_counter()
            await asyncio.to_thread(compose, args.workdir, "up", "--detach", "--build", "--remove-orphans")
            result["post_up_ready_poll_ms"] = await wait_ready(args.workdir, args.url, args.startup_timeout)
            result["up_to_ready_ms_including_build"] = (time.perf_counter() - started) * 1000
            defaults = specification["defaults"]
            await asyncio.to_thread(seed, args.workdir, defaults["rows"], defaults["tenants"], defaults["payload_bytes"], job["target"] == "supabase")
            await asyncio.to_thread(lock, args.workdir)
            trial = await run(run_namespace(args.workdir, path, job, args.url))
            result["completed"] = trial["completed"]
        except Exception as error:
            result.update(completed=False, failure_type=type(error).__name__)
            dump(path.with_suffix(".failure.json"), result)
        finally:
            stop.set()
            if collector:
                try:
                    await collector
                except Exception as error:
                    result["collector_failure"] = type(error).__name__
            result["finished_unix"] = time.time()
            outcomes.append(result)
            dump(output / "campaign-status.json", {"outcomes": outcomes, "specification": str(args.file)})
        if not result.get("completed") and args.stop_on_failure:
            break
    return {"jobs_attempted": len(outcomes), "completed": sum(bool(r.get("completed")) for r in outcomes),
            "status": str(output / "campaign-status.json")}


def expand_cpuset(value):
    cpus = set()
    for part in value.split(","):
        bits = part.split("-")
        if len(bits) == 1:
            cpus.add(int(bits[0]))
        elif len(bits) == 2:
            begin, end = map(int, bits)
            if begin > end:
                raise ValueError("invalid CPU range")
            cpus.update(range(begin, end + 1))
        else:
            raise ValueError("invalid CPU list")
    if not cpus or min(cpus) < 0:
        raise ValueError("invalid CPU list")
    return sorted(cpus)

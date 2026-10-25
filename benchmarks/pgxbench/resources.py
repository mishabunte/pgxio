"""Linux host-side cgroup v2/PSS collection. Never docker-exec a sampler."""
import asyncio
import json
from pathlib import Path
import platform
import time

from .common import command, compose, load_settings, public_settings


def key_values(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                result[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                pass  # smaps_rollup starts with a nonnumeric mapping header.
    return result


def read_group(path):
    path = Path(path)
    current = int((path / "memory.current").read_text())
    peak_path = path / "memory.peak"
    result = {"memory_current_bytes": int((path / "memory.current").read_text()),
              "memory_peak_bytes": int(peak_path.read_text()) if peak_path.exists() else current,
              "memory_peak_scope": "lifetime" if peak_path.exists() else "sampled_current_only",
              "memory_stat": key_values(path / "memory.stat"),
              "memory_events": key_values(path / "memory.events"),
              "cpu_stat": key_values(path / "cpu.stat"),
              "memory_max": (path / "memory.max").read_text().strip(),
              "swap_max": (path / "memory.swap.max").read_text().strip(),
              "cpu_max": (path / "cpu.max").read_text().strip(),
              "cpuset_effective": (path / "cpuset.cpus.effective").read_text().strip()}
    pids = set()
    for entry in [path / "cgroup.procs", *path.glob("**/cgroup.procs")]:
        try:
            pids.update(int(value) for value in entry.read_text().split())
        except FileNotFoundError:
            pass
    result.update(pss_bytes=0, rss_bytes=0, process_count=0)
    for pid in pids:
        try:
            values = key_values(Path("/proc") / str(pid) / "smaps_rollup")
        except (FileNotFoundError, ProcessLookupError):
            continue
        result["pss_bytes"] += values["Pss"] * 1024
        result["rss_bytes"] += values["Rss"] * 1024
        result["process_count"] += 1
    pressure = path / "memory.pressure"
    if pressure.exists():
        result["memory_pressure"] = pressure.read_text().strip()
    return result


def discover(workdir):
    ids = compose(workdir, "ps", "--quiet").splitlines()
    if not ids:
        return {}
    records = json.loads(command(["docker", "inspect", *ids]))
    groups = {}
    for record in records:
        labels = record["Config"]["Labels"]
        if labels.get("com.docker.compose.project") != "pgxbench":
            raise ValueError("collector only accepts the isolated pgxbench project")
        pid = record["State"]["Pid"]
        if not pid:
            continue
        lines = (Path("/proc") / str(pid) / "cgroup").read_text().splitlines()
        relative = next((line[3:] for line in lines if line.startswith("0::")), None)
        if relative is None:
            raise RuntimeError("cgroup v2 is required")
        path = Path("/sys/fs/cgroup") / relative.lstrip("/")
        service = labels["com.docker.compose.service"]
        groups[service] = {"path": str(path), "container_id": record["Id"], "image_id": record["Image"],
                           "pid": pid,
                           "role": labels.get("pgxbench.role"),
                           "limits": {key: record["HostConfig"].get(key)
                                      for key in ("Memory", "MemorySwap", "NanoCpus", "CpusetCpus", "CgroupParent")}}
    return groups


def host_sample():
    memory = key_values("/proc/meminfo")
    vm = key_values("/proc/vmstat")
    return {"memory_total_bytes": memory["MemTotal"] * 1024,
            "memory_available_bytes": memory["MemAvailable"] * 1024,
            "swap_used_bytes": (memory["SwapTotal"] - memory["SwapFree"]) * 1024,
            "pswpin": vm["pswpin"], "pswpout": vm["pswpout"],
            "oom_kill": vm.get("oom_kill", 0),
            "cpu_stat": Path("/proc/stat").read_text().splitlines()[0],
            "load_average": [float(v) for v in Path("/proc/loadavg").read_text().split()[:3]]}


async def collect(workdir, output, interval=1, duration=0, stop=None):
    if platform.system() != "Linux":
        raise RuntimeError("run the collector on the Linux Docker host, not macOS or the load generator")
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        raise RuntimeError("Linux cgroup v2 is required")
    settings = load_settings(workdir)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    refresh_at = 0
    groups = {}
    parent_peak = None
    if settings.get("cgroup_path"):
        # memory.peak reset applies to this open FD, not subsequent read_text
        # opens. Keep it open across the trial to avoid previous-trial peaks.
        try:
            parent_peak = (Path(settings["cgroup_path"]) / "memory.peak").open("r+")
            parent_peak.write("1")
            parent_peak.flush()
        except OSError:
            if parent_peak:
                parent_peak.close()
            parent_peak = None  # Older kernels expose a read-only lifetime peak.
    with output.open("w") as stream:
        stream.write(json.dumps({"type": "metadata", "timestamp": time.time(),
            "configuration": public_settings(settings), "platform": platform.platform(),
            "cpu_info": Path("/proc/cpuinfo").read_text(), "collector_interval": interval}) + "\n")
        stream.flush()
        while (not duration or time.monotonic() - started < duration) and not (stop and stop.is_set()):
            sample = {"type": "sample", "timestamp": time.time(), "monotonic_seconds": time.monotonic(),
                      "groups": {}, "errors": []}
            try:
                if time.monotonic() >= refresh_at:
                    groups = await asyncio.to_thread(discover, workdir)
                    refresh_at = time.monotonic() + 5
                    sample["containers"] = groups
                for name, info in groups.items():
                    try:
                        sample["groups"][name] = {"role": info["role"], "container_id": info["container_id"],
                                                  **read_group(info["path"])}
                    except FileNotFoundError:
                        sample["errors"].append(name + ":container_disappeared")
                parent = settings.get("cgroup_path")
                if parent:
                    sample["api_parent"] = read_group(parent)
                    if parent_peak:
                        parent_peak.seek(0)
                        sample["api_parent"]["memory_peak_bytes"] = int(parent_peak.read())
                        sample["api_parent"]["memory_peak_scope"] = "collector_window"
                sample["host"] = host_sample()
            except (OSError, ValueError, RuntimeError) as error:
                sample["errors"].append(type(error).__name__ + ":collector_requires_host_proc_and_smaps_permissions")
            stream.write(json.dumps(sample) + "\n")
            stream.flush()
            if stop:
                try:
                    await asyncio.wait_for(stop.wait(), interval)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)
    if parent_peak:
        parent_peak.close()
    return {"resource_file": str(output)}


async def collect_colima(workdir, output, stop, ready):
    """Stream host /proc/cgroup samples over one SSH session; no docker exec."""
    ids = (await asyncio.to_thread(compose, workdir, 'ps', '--quiet')).splitlines()
    records = json.loads(await asyncio.to_thread(command, ['docker', 'inspect', *ids]))
    groups = {}
    for record in records:
        labels = record['Config']['Labels']
        if labels.get('com.docker.compose.project') != 'pgxbench':
            raise ValueError('refusing to sample another Compose project')
        groups[labels['com.docker.compose.service']] = {
            'pid': record['State']['Pid'], 'container_id': record['Id'], 'role': labels['pgxbench.role']}
    if not groups or not any(g['role'] == 'api' for g in groups.values()):
        raise RuntimeError('no benchmark API to sample')
    worker = Path(__file__).with_name('resource_worker.py').resolve()
    process = await asyncio.create_subprocess_exec('colima', 'ssh', '--', 'sudo', '-n', 'python3', '-P', str(worker),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    async def end_input():
        await stop.wait()
        process.stdin.close()
    watcher = asyncio.create_task(end_input())
    try:
        process.stdin.write((json.dumps(groups) + '\n').encode())
        await process.stdin.drain()
        with path.open('w') as stream:
            stream.write(json.dumps({'type': 'metadata', 'timestamp': time.time(),
                'configuration': public_settings(load_settings(workdir)), 'collector_interval': 1,
                'collector_host': 'colima-linux-vm', 'scope': 'host-side /proc and cgroup v2'}) + '\n')
            while line := await process.stdout.readline():
                sample = json.loads(line)
                if sample.get('errors'):
                    raise RuntimeError('host resource sampling failed: ' + ','.join(sample['errors']))
                stream.write(json.dumps(sample) + '\n')
                stream.flush()
                ready.set()
        if await process.wait():
            diagnostic = (await process.stderr.read()).decode(errors='replace')[-2000:]
            raise RuntimeError('Colima resource sampler exited unsuccessfully: ' + diagnostic)
    finally:
        watcher.cancel()
        if process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.terminate()
                await process.wait()


def apply_limits(path, cpus, memory_mib=0, cpuset=None):
    root = Path("/sys/fs/cgroup").resolve()
    path = Path(path).resolve()
    if path == root or root not in path.parents or not (path / "cgroup.procs").exists():
        raise ValueError("provide an existing dedicated child cgroup under /sys/fs/cgroup; never the root")
    if cpus < 1 or memory_mib < 0:
        raise ValueError("invalid resource limits")
    (path / "cpu.max").write_text(f"{int(cpus * 100000)} 100000")
    (path / "memory.swap.max").write_text("0")
    (path / "memory.max").write_text(str(memory_mib * 1024 * 1024) if memory_mib else "max")
    if cpuset:
        (path / "cpuset.cpus").write_text(cpuset)
    return {"cgroup": str(path), "cpu_max": (path / "cpu.max").read_text().strip(),
            "memory_max": (path / "memory.max").read_text().strip(), "swap_max": "0"}


def summarize(path, run):
    """Summarize only this trial, refusing missing, stale or foreign evidence."""
    metadata = None
    samples = []
    first = {}
    last = {}
    peaks = {"api_pss_bytes": 0, "api_cgroup_current_bytes": 0,
             "database_pss_bytes": 0, "database_cgroup_current_bytes": 0,
             "host_used_bytes": 0, "api_parent_peak_bytes": 0}
    errors = set()
    containers = set()
    enforced = True
    first_host = last_host = None
    measured = []
    sample_times = {}
    config = run["configuration"]
    cap = config.get("memory_mib", 0) * 1048576
    expected_services = {"pgxio": {"pgxio"}, "node": {"node"}, "go": {"go"}, "openresty": {"openresty"},
                         "postgrest": {"postgrest", "proxy"}, "supabase": {"postgrest", "proxy", "realtime"}}
    begin, end = run["started_unix"], run["finished_unix"]
    load_begin = run.get("measurement_started_unix", begin)
    load_end = run.get("measurement_finished_unix", end)
    with Path(path).open() as stream:
        for line in stream:
            item = json.loads(line)
            if item["type"] == "metadata":
                metadata = item
                continue
            if not begin <= item["timestamp"] <= end:
                continue
            samples.append(item["timestamp"])
            errors.update(item.get("errors", []))
            api = [g for g in item["groups"].values() if g["role"] == "api"]
            db = [g for g in item["groups"].values() if g["role"] == "database"]
            if not api:
                errors.add("missing_api_samples")
            observed = {name for name,g in item["groups"].items() if g["role"] == "api"}
            required = expected_services.get(run.get("target", config.get("target")), observed)
            if config.get("target") == "postgrest" and config.get("frontend") == "http":
                required = {"postgrest"}
            if observed != required:
                errors.add("missing_or_extra_API_component")
            if cap:
                if config.get("cgroup_path"):
                    parent = item.get("api_parent", {})
                    enforced = enforced and parent.get("memory_max") == str(cap) and parent.get("swap_max") == "0"
                else:
                    enforced = enforced and len(api) == 1 and api[0]["memory_max"] == str(cap) and api[0]["swap_max"] == "0"
            for prefix, values in (("api", api), ("database", db)):
                peaks[prefix + "_pss_bytes"] = max(peaks[prefix + "_pss_bytes"], sum(g["pss_bytes"] for g in values))
                peaks[prefix + "_cgroup_current_bytes"] = max(peaks[prefix + "_cgroup_current_bytes"],
                                                              sum(g["memory_current_bytes"] for g in values))
            if "host" in item:
                host = item["host"]
                first_host = first_host or host
                last_host = host
                peaks["host_used_bytes"] = max(peaks["host_used_bytes"], host["memory_total_bytes"] - host["memory_available_bytes"])
                if host["swap_used_bytes"] or host["pswpout"]:
                    # Historical swap activity is separated below; swap used is a direct concern.
                    if host["swap_used_bytes"]:
                        errors.add("host_swap_in_use")
            if "api_parent" in item:
                parent = item["api_parent"]
                value = parent["memory_peak_bytes"] if parent.get("memory_peak_scope") == "collector_window" else parent["memory_current_bytes"]
                peaks["api_parent_peak_bytes"] = max(peaks["api_parent_peak_bytes"], value)
            if load_begin <= item["timestamp"] <= load_end:
                measured.append(item)
                for name, group in item["groups"].items():
                    identity = (name, group["container_id"])
                    containers.add(identity)
                    first.setdefault(identity, group)
                    clock = item.get('monotonic_seconds', item['timestamp'])
                    sample_times.setdefault(identity, [clock, clock])[1] = clock
                    last[identity] = group
    if not metadata or metadata["configuration"] != run["configuration"]:
        errors.add("configuration_mismatch")
    if len(samples) < 2:
        errors.add("insufficient_samples")
    if samples and (samples[0] - begin > 5 or end - samples[-1] > 5):
        errors.add("incomplete_time_coverage")
    if len(samples) > 1 and max(b-a for a,b in zip(samples,samples[1:])) > 5:
        errors.add("resource_sampling_gap")
    if cap and not enforced:
        errors.add("requested_memory_cap_not_enforced")
    if first_host and last_host and (last_host["pswpout"] > first_host["pswpout"] or last_host["pswpin"] > first_host["pswpin"]):
        errors.add("host_swapped_during_trial")
    if first_host and last_host and last_host.get("oom_kill", 0) > first_host.get("oom_kill", 0):
        errors.add("host_OOM_during_trial")
    api_cpu = db_cpu = 0
    cpu_percent = {"api": 0.0, "database": 0.0}
    for identity in containers:
        a, b = first[identity], last[identity]
        cpu = max(0, b["cpu_stat"]["usage_usec"] - a["cpu_stat"]["usage_usec"]) / 1e6
        seconds = sample_times[identity][1] - sample_times[identity][0]
        if seconds > 0:
            cpu_percent[b["role"]] += 100 * cpu / seconds
        if b["role"] == "api":
            api_cpu += cpu
        else:
            db_cpu += cpu
        if b["memory_events"].get("oom", 0) or b["memory_events"].get("oom_kill", 0):
            errors.add("oom_event")
    names = [name for name, _ in containers]
    if len(names) != len(set(names)):
        errors.add("container_restarted_during_measurement")
    averages, measured_peaks = {}, {}
    for role in ("api", "database"):
        for metric in ("pss_bytes", "rss_bytes", "memory_current_bytes"):
            values = [sum(g.get(metric, 0) for g in sample["groups"].values() if g["role"] == role)
                      for sample in measured]
            averages[role + "_" + metric] = sum(values) / len(values) if values else None
            measured_peaks[role + "_" + metric] = max(values) if values else None
        averages[role + "_cpu_percent"] = cpu_percent[role] if measured else None
    loads = [sample["host"]["load_average"][0] for sample in measured
             if sample.get("host", {}).get("load_average")]
    averages["host_load1"] = sum(loads) / len(loads) if loads else None
    if len(measured) < 2:
        errors.add("insufficient_measurement_samples")
    success = run["results"]["counts"].get("http_200", 0)
    return {"source": str(path), "valid": not errors, "errors": sorted(errors), "samples": len(samples),
            "peaks": peaks, "averages": averages, "measurement_peaks": measured_peaks, "api_cpu_seconds": api_cpu, "database_cpu_seconds": db_cpu,
            "requested_memory_cap_enforced": bool(cap and enforced and samples),
            "host_memory_total_bytes": last_host["memory_total_bytes"] if last_host else None,
            "api_cpu_ms_per_success": api_cpu * 1000 / success if success else None,
            "note": "summed container peaks are not a simultaneous aggregate peak; use parent peak for shared RAM budgets"}

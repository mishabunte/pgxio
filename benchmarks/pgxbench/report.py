import csv
import hashlib
import json
from pathlib import Path
from statistics import median

from .common import ROOT, dump
from .resources import summarize


def assess(run, resources=None):
    counts = run.get("results", {}).get("counts", {})
    attempted = sum(v for k, v in counts.items() if k.startswith("http_") or k.startswith("transport_") or k == "invalid_response")
    failures = attempted - counts.get("http_200", 0)
    missed = sum(counts.get(k, 0) for k in ("unscheduled", "dropped_inflight_limit", "probe_unscheduled"))
    reasons = []
    if not run.get("completed") or not run.get("preflight", {}).get("passed"):
        reasons.append("incomplete_or_preflight_failed")
    if not attempted or failures / attempted > .001:
        reasons.append("HTTP_success_below_99.9_percent")
    if missed:
        reasons.append("offered_load_not_scheduled")
    if counts.get("offered_minus_completed", 0) > .001 * max(1, attempted + counts.get("offered_minus_completed", 0)):
        reasons.append("achieved_throughput_below_99.9_percent_of_wrk2_offered_rate")
    p95 = run.get("results", {}).get("http_latency", {}).get("p95_ms")
    if p95 is None or p95 >= 100:
        reasons.append("HTTP_p95_at_or_above_100ms")
    workload = run.get("workload", {})
    if workload.get("sockets", 0):
        p99 = run["results"]["notification_latency"]["p99_ms"]
        if p99 is None or p99 >= 1000:
            reasons.append("notification_p99_at_or_above_1s")
        if any(counts.get(key, 0) for key in ("probe_timeout", "probe_write_error", "missing_notifications",
            "missing_fresh_snapshots", "snapshot_error", "stale_snapshot", "ws_unexpected_close_or_error", "ws_heartbeat_error")):
            reasons.append("realtime_delivery_or_freshness_failure")
        if workload.get("realtime_mode") != "notify" and not run["results"]["fresh_data_latency"]["count"]:
            reasons.append("missing_freshness_measurement")
    if resources and not resources["valid"]:
        reasons.append("invalid_resource_evidence")
    return {"workload_passed": not reasons, "reasons": reasons,
            "HTTP_error_fraction": failures / attempted if attempted else None,
            "resource_evidence_valid": bool(resources and resources["valid"]),
            "minimum_RAM_qualified": False}


def make_report(directory, output, include_functional=False):
    directory = Path(directory)
    rows, trials = [], []
    for path in sorted(directory.glob("**/*.json")):
        try:
            run = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if run.get("kind") != "competitive_benchmark":
            continue
        if run.get("purpose") == "functional" and not include_functional:
            continue
        resource_path = path.with_suffix(".resources.jsonl")
        resources = summarize(resource_path, run) if resource_path.exists() else None
        if resources:
            export_resource_samples(resource_path, Path(output) / "resource-samples" / path.relative_to(directory).with_suffix(".csv"))
        assessment = assess(run, resources)
        c, w, r = run["configuration"], run["workload"], run["results"]
        row = {"output_validation": run.get("output_validation", "legacy"), "rest_input_validation": run.get("rest_input_validation", "legacy"), "request_bytes": w.get("request_bytes", 0), "performance_profile": c.get("performance_profile") or "unspecified", "nginx_profile": c.get("nginx_profile", "baseline"), "target": run["target"], "cpus": c["cpus"], "workers": c["workers"],
               "pool_mode": c["pool_mode"], "pool_total": c["pool_total"], "memory_mib": c["memory_mib"],
               "workload": w["workload"], "offered_background_rate": w.get("background_rate", w["rate"]),
               "sockets": w["sockets"], "subscriptions": w["subscriptions"], "transport": run["transport"],
               "engine": run.get("engine", "python"),
               "auth_mode": c.get('auth_mode', 'jwt'), "auth_fixture": c.get('auth_fixture', 'tenant'),
               "database_tls": c.get('database_tls', 'verify-full'),
               "jwt_cache": c.get('jwt_cache', 'default'),
               "connections": run.get("wrk", {}).get("connections"),
               "load_threads": run.get("wrk", {}).get("threads"),
               "HTTP_error_fraction": assessment["HTTP_error_fraction"],
               "http_503": r["counts"].get("http_503", 0),
               "unscheduled": r["counts"].get("unscheduled", 0),
               "success_rps": r["successful_requests_per_second"], "http_p95_ms": r["http_latency"]["p95_ms"],
               "http_p99_ms": r["http_latency"]["p99_ms"], "notify_p99_ms": r["notification_latency"]["p99_ms"],
               "fresh_p99_ms": r["fresh_data_latency"]["p99_ms"], "workload_passed": assessment["workload_passed"],
               "resource_evidence_valid": assessment["resource_evidence_valid"], "source": str(path)}
        for name in ("api_pss_bytes", "api_cgroup_current_bytes", "database_pss_bytes", "host_used_bytes", "api_parent_peak_bytes"):
            row[name] = resources["peaks"][name] if resources else None
        for name in ("api_pss_bytes", "api_rss_bytes", "api_memory_current_bytes", "database_pss_bytes",
                     "database_memory_current_bytes", "api_cpu_percent", "database_cpu_percent", "host_load1"):
            row[name + "_mean"] = resources.get("averages", {}).get(name) if resources else None
        for name in ("api_pss_bytes", "api_memory_current_bytes", "database_pss_bytes"):
            row[name + "_load_peak"] = resources.get("measurement_peaks", {}).get(name) if resources else None
        row["api_cpu_ms_per_success"] = resources["api_cpu_ms_per_success"] if resources else None
        profile_key = json.dumps({"target": run["target"], "configuration": c, "workload": w,
            "images": run.get("image_lock"), "engine": run.get("engine", "python"),
            "engine_settings": {k: run.get("wrk", {}).get(k) for k in ("connections", "threads", "engine_identity")},
            "transport": run["transport"]}, sort_keys=True)
        row['profile_id'] = hashlib.sha256(profile_key.encode()).hexdigest()
        rows.append(row)
        trials.append({"source": str(path), "assessment": assessment, "resources": resources,
                       "target": run["target"], "configuration": c, "workload": w,
                       "duration": w["duration"], "image_lock": run.get("image_lock"),
                       "transport": run["transport"], "engine": run.get("engine", "python"),
                       "engine_settings": {k: run.get("wrk", {}).get(k) for k in ("connections", "threads", "engine_identity")},
                       "reconnect_verified": run.get("reconnect_verified", False)})
        trials[-1]["measured_elapsed_seconds"] = run.get("measured_elapsed_seconds", 0)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    dump(output / "summary.json", {"trials": trials, "rows": rows,
         "warning": "Load-only passes do not establish a RAM floor. Compare only matching feature/fixture/resource profiles."})
    if rows:
        with (output / "summary.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    text = ["# Competitive benchmark results", "",
            "Load-only passes do not qualify minimum RAM. Native feature differences remain part of the comparison.", "",
            "| Target | Stage | Nginx | Auth | Fixture | JWT cache | vCPU | Workload | Connections | Sockets | Successful RPS | HTTP errors % | HTTP p95 ms | Event p99 ms | Fresh p99 ms | API peak PSS MiB | API mean PSS MiB | API mean CPU % | DB mean CPU % | Mean host load1 | Load pass | Resources |",
            "|---|---|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    def fmt(value):
        return "N/A" if value is None else f"{value:.2f}"
    for row in rows:
        pss = row["api_pss_bytes"] / 1048576 if row["api_pss_bytes"] is not None else None
        error_percent = row['HTTP_error_fraction'] * 100 if row['HTTP_error_fraction'] is not None else None
        text.append(f"| {row['target']} | {row['performance_profile']} | {row['nginx_profile']} | {row['auth_mode']} | {row['auth_fixture']} | {row['jwt_cache']} | {row['cpus']} | {row['workload']} | {row['connections'] or 'N/A'} | {row['sockets']} | {fmt(row['success_rps'])} | {fmt(error_percent)} | "
                    f"{fmt(row['http_p95_ms'])} | {fmt(row['notify_p99_ms'])} | {fmt(row['fresh_p99_ms'])} | "
                    f"{fmt(pss)} | {fmt(row['api_pss_bytes_mean'] / 1048576 if row['api_pss_bytes_mean'] is not None else None)} | "
                    f"{fmt(row['api_cpu_percent_mean'])} | {fmt(row['database_cpu_percent_mean'])} | {fmt(row['host_load1_mean'])} | {row['workload_passed']} | {row['resource_evidence_valid']} |")
    # Group by all experimental variables, not merely target/CPU. Do not average percentiles.
    groups = {}
    for row, trial in zip(rows, trials):
        key = json.dumps({"target": trial["target"], "configuration": trial["configuration"],
                          "workload": trial["workload"], "images": trial["image_lock"],
                          "engine": trial["engine"], "engine_settings": trial["engine_settings"],
                          "transport": trial["transport"]}, sort_keys=True)
        groups.setdefault(key, []).append(row)
    aggregates = []
    for key, group in groups.items():
        values = [r["success_rps"] for r in group]
        aggregates.append({"profile": json.loads(key), "trials": len(group), "success_rps_median": median(values),
                           "success_rps_min": min(values), "success_rps_max": max(values),
                           "all_load_passed": all(r["workload_passed"] for r in group)})
    dump(output / "aggregates.json", aggregates)
    (output / "summary.md").write_text("\n".join(text) + "\n")
    chart = (ROOT / "pgxbench" / "charts.html").read_text()
    chart = chart.replace("DATA_PLACEHOLDER", json.dumps(rows).replace("<", "\\u003c"))
    (output / "charts.html").write_text(chart)
    return {"trials": len(rows), "report": str(output / "summary.md")}


def qualify(directory, output):
    """Require three one-hour production-profile trials at an enforced RAM cap."""
    temporary_report = Path(output).parent / "qualification-evidence"
    make_report(directory, temporary_report)
    trials = json.loads((temporary_report / "summary.json").read_text())["trials"]
    groups, host_groups = {}, {}
    for trial in trials:
        c, w = trial["configuration"], trial["workload"]
        realtime_profile = (w["workload"] == "small-app" and w["sockets"] == 100
                            and w["active_tenants"] == 10 and w["subscriptions"] == 1 and w["realtime_mode"] != "notify")
        rest_profile = w["workload"] == "page" and w["sockets"] == 0
        eligible = ((rest_profile or (realtime_profile and trial["reconnect_verified"])) and w["rate"] == 50 and w["duration"] >= 3600
                    and trial["measured_elapsed_seconds"] >= 3600
                    and c["cpus"] == 1
                    and c["rows"] == 10000 and c["tenants"] == 100 and c["payload_bytes"] == 128
                    and trial["assessment"]["resource_evidence_valid"] and trial["image_lock"])
        if not eligible:
            continue
        profile = dict(c)
        memory = profile.pop("memory_mib")
        host_memory = profile.pop("vm_memory_mib", 0)
        profile["qualification_workload"] = "REST_50rps" if rest_profile else "REST_50rps_realtime_100"
        profile["transport"] = trial["transport"]
        key = json.dumps({"target": trial["target"], "profile": profile, "images": trial["image_lock"]}, sort_keys=True)
        if memory > 0 and trial["resources"]["requested_memory_cap_enforced"]:
            groups.setdefault(key, {}).setdefault(memory, []).append(trial)
        if host_memory:
            actual = trial["resources"].get("host_memory_total_bytes") or 0
            if abs(actual - host_memory * 1048576) <= max(32 * 1048576, host_memory * 1048576 * .1):
                host_groups.setdefault(key, {}).setdefault(host_memory, []).append(trial)
    floors = []
    for key, candidates in groups.items():
        passing = [memory for memory, runs in candidates.items()
                   if len(runs) >= 3 and all(t["assessment"]["workload_passed"] for t in runs)]
        if not passing:
            continue
        lowest = min(passing)
        # A lowest observed pass is not a minimum until the adjacent 16 MiB
        # candidate was tested and failed. The search boundary is always explicit.
        below = candidates.get(lowest - 16, [])
        bounded = bool(below and any(not t["assessment"]["workload_passed"] for t in below))
        floors.append({"profile": json.loads(key), "lowest_observed_passing_api_mib": lowest,
                       "minimum_api_mib": lowest if bounded else None,
                       "boundary_verified_at_16_mib_resolution": bounded,
                       "recommendation_candidate_mib": math_ceil_multiple(lowest * 1.25, 16),
                       "recommended_RAM_verified": False,
                       "note": "Recommendation still needs startup/reload evidence and a 24-hour soak. Whole-VPS minima require actual VM RAM trials."})
    host_floors = []
    for key, candidates in host_groups.items():
        passing = [memory for memory,runs in candidates.items()
                   if len(runs) >= 3 and all(t["assessment"]["workload_passed"] for t in runs)]
        if not passing:
            continue
        lowest = min(passing)
        bounded = any(not t["assessment"]["workload_passed"] for t in candidates.get(lowest - 64, []))
        profile = json.loads(key)
        host_floors.append({"profile": profile, "topology": "external_database" if profile["profile"]["external_database"] else "local_PostgreSQL",
            "lowest_observed_passing_VPS_mib": lowest, "minimum_VPS_mib": lowest if bounded else None,
            "boundary_verified_at_64_mib_resolution": bounded,
            "recommendation_candidate_mib": math_ceil_multiple(lowest * 1.25, 64), "recommended_RAM_verified": False})
    result = {"api_floors": floors, "whole_VPS_floors": host_floors,
              "note": "No service-cgroup result is relabeled as minimum VPS RAM. Startup/reload and 24-hour confirmation remain separate evidence."}
    dump(output, result)
    return result


def math_ceil_multiple(value, multiple):
    import math
    return math.ceil(value / multiple) * multiple


def export_resource_samples(source, destination):
    """Keep warmup, load, shutdown and retention samples as a plottable CSV."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Path(source).open() as stream, destination.open("w", newline="") as target:
        fields = ["timestamp", "api_pss_bytes", "api_cgroup_bytes", "database_pss_bytes", "database_cgroup_bytes",
                  "api_cpu_usage_usec", "database_cpu_usage_usec", "host_available_bytes", "host_load1", "errors"]
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for line in stream:
            sample = json.loads(line)
            if sample["type"] != "sample":
                continue
            row = {"timestamp": sample["timestamp"], "errors": ";".join(sample.get("errors", [])),
                   "host_available_bytes": sample.get("host", {}).get("memory_available_bytes")}
            row['host_load1'] = sample.get('host', {}).get('load_average', [None])[0]
            for prefix, role in (("api", "api"), ("database", "database")):
                groups = [g for g in sample["groups"].values() if g["role"] == role]
                row[prefix + "_pss_bytes"] = sum(g["pss_bytes"] for g in groups)
                row[prefix + "_cgroup_bytes"] = sum(g["memory_current_bytes"] for g in groups)
                row[prefix + "_cpu_usage_usec"] = sum(g["cpu_stat"]["usage_usec"] for g in groups)
            writer.writerow(row)

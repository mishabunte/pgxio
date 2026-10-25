"""Bounded open-arrival HTTP driver and pgxio/Phoenix WebSocket adapters.

All timing uses the load generator's monotonic clock. Versioned snapshots make
fresh-data latency meaningful even when notifications are coalesced.
"""
import asyncio
from collections import Counter
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import platform
import random
import ssl
import time

import httpx
from websockets.asyncio.client import connect
from urllib.parse import urlencode

from .common import dump, load_settings, public_settings, route, token


class Histogram:
    """Logarithmic histogram: <=1% quantization, bounded memory for long soaks."""
    def __init__(self):
        self.bins = Counter()
        self.count = 0
        self.maximum = None

    def add(self, milliseconds):
        value = max(0, milliseconds)
        self.bins[int(math.log1p(value * 1000) / math.log(1.01))] += 1
        self.count += 1
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def quantile(self, fraction):
        if not self.count:
            return None
        threshold = max(1, math.ceil(self.count * fraction))
        count = 0
        for bucket, size in sorted(self.bins.items()):
            count += size
            if count >= threshold:
                return min(self.maximum, math.expm1((bucket + .5) * math.log(1.01)) / 1000)

    def summary(self):
        return {"count": self.count, "p50_ms": self.quantile(.5), "p95_ms": self.quantile(.95),
                "p99_ms": self.quantile(.99), "max_ms": self.maximum,
                "histogram_base": 1.01, "histogram_unit": "microseconds_log1p",
                "histogram": dict(self.bins)}


class Metrics:
    def __init__(self):
        self.counts = Counter()
        self.http = Histogram()
        self.http_success = Histogram()
        self.schedule = Histogram()
        self.notify = Histogram()
        self.fresh = Histogram()
        self.operations = Counter()

    def summary(self, elapsed):
        return {"counts": dict(self.counts), "operations": dict(self.operations),
                "successful_requests_per_second": self.counts["http_200"] / elapsed if elapsed else 0,
                "http_latency": self.http.summary(), "successful_http_latency": self.http_success.summary(),
                "scheduler_delay": self.schedule.summary(), "notification_latency": self.notify.summary(),
                "fresh_data_latency": self.fresh.summary()}


@dataclass
class Probe:
    started: float
    expected: set
    fresh_expected: set
    version: int = 0
    version_ready: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    deliveries: dict = field(default_factory=dict)


class Driver:
    def __init__(self, args):
        self.args = args
        self.settings = load_settings(args.workdir)
        self.target = args.target or self.settings["target"]
        for field, default in (('auth_mode', 'jwt'), ('auth_fixture', 'tenant')):
            configured = self.settings.get(field, default)
            requested = getattr(args, field, None)
            if requested is not None and requested != configured:
                raise ValueError(field + ' must match the rendered server configuration')
            setattr(args, field, configured)
        self.public_fixture = args.auth_fixture == 'public'
        if args.sockets and (self.public_fixture or args.auth_mode == 'none'):
            raise ValueError('authentication-cost profiles are REST-only; realtime uses the tenant JWT profile')
        self.native = self.target in ("postgrest", "supabase")
        self.rest_input_validation = 'native' if self.native else 'schema'
        self.output_validation = 'native' if self.native else 'schema'
        if self.target == 'pgxio':
            self.output_validation = 'off' if self.settings.get('effective_config_format') == 'nginx-v1' else 'schema'
            output_option = self.settings.get('module_options', {}).get('validate_output')
            if output_option is not None:
                self.output_validation = 'schema' if output_option else 'off'
            configured = self.settings.get('module_options', {}).get('validate_rest_input')
            lockfile = Path(args.workdir) / 'images.lock.json'
            default = 'unknown'
            if lockfile.exists():
                images = json.loads(lockfile.read_text()).get('images', [])
                default = next((image.get('rest_input_validation_default', 'schema')
                    for image in images if image['service'] == 'pgxio'), 'unknown')
            self.rest_input_validation = ('schema' if configured else 'postgres') if configured is not None else default
        self.url = args.url.rstrip("/")
        self.ws_url = args.ws_url or self.url.replace("https://", "wss://").replace("http://", "ws://") + (
            "/realtime/v1/websocket?vsn=1.0.0" if self.target == "supabase" else "/v1/events")
        self.tokens = [token(args.workdir, i, max(172800, math.ceil(args.duration + args.warmup + 3600)))
                       for i in range(max(2, args.active_tenants))]
        self.metrics = Metrics()
        self.probes = {}
        self.tasks = set()
        self.sockets = []
        self.clients = []
        self.running = True
        self.setup_snapshot_retries = 0
        self.random = random.Random(args.seed)
        ca = args.ca or str(Path(args.workdir) / "runtime" / "tls-cert.pem")
        if self.url.startswith("https:") and not getattr(ssl, "HAS_TLSv1_3", False):
            raise RuntimeError("benchmark HTTPS requires TLS 1.3; use Python linked to OpenSSL 1.1.1+ (macOS Xcode LibreSSL is too old), or HTTP for a functional check")
        self.ssl = ssl.create_default_context(cafile=ca) if self.url.startswith("https:") else None
        self.client = httpx.AsyncClient(verify=self.ssl or True, timeout=args.timeout,
            limits=httpx.Limits(max_connections=args.max_inflight, max_keepalive_connections=args.max_inflight),
            trust_env=False)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def call(self, operation, body, tenant=0, scheduled=None, jwt=None, measure=True, private=False, omit_auth=False):
        start = time.perf_counter()
        if measure and scheduled is not None:
            self.metrics.schedule.add((start - scheduled) * 1000)
        actual_operation = operation
        if self.public_fixture and not private:
            actual_operation = operation.replace('bench_', 'bench_public_')
            from .common import subject
            body = {**body, 'tenant': subject(tenant)}
        path = ("/rpc/" if self.native else "/v1/") + actual_operation
        headers = {}
        if not omit_auth and (self.args.auth_mode == 'jwt' or private or jwt is not None):
            headers['Authorization'] = 'Bearer ' + (self.tokens[tenant] if jwt is None else jwt)
        if self.args.api_key_env:
            import os
            headers["apikey"] = os.environ[self.args.api_key_env]
        try:
            payload = body
            request_bytes = getattr(self.args, 'request_bytes', 0)
            if request_bytes:
                raw = json.dumps({**payload, 'padding':''}, separators=(',',':')).encode()
                remaining = request_bytes - len(raw)
                if remaining < 0:
                    raise ValueError('requested body size too small')
                payload = {**payload, 'padding':'x' * remaining}
                raw = json.dumps(payload, separators=(',',':')).encode()
                if len(raw) != request_bytes:
                    raise ValueError('request size mismatch')
                # Match wrk: size the SQL input, then add PostgREST's envelope.
                if self.native:
                    raw = b'{"input":' + raw + b'}'
                headers['Content-Type'] = 'application/json'
                response = await self.client.post(self.url + path, content=raw, headers=headers)
            else:
                if self.native:
                    payload = {"input": payload}
                response = await self.client.post(self.url + path, json=payload, headers=headers)
            status = "http_" + str(response.status_code)
            try:
                value = response.json()
            except ValueError:
                value = None
            if response.status_code == 200 and not valid_response(operation, value):
                status = "invalid_response"
        except httpx.HTTPError as error:
            status, value = "transport_" + type(error).__name__, None
        if measure:
            latency = (time.perf_counter() - start) * 1000
            self.metrics.http.add(latency)
            self.metrics.counts[status] += 1
            self.metrics.operations[operation] += 1
            if status == "http_200":
                self.metrics.http_success.add(latency)
        return status, value

    async def preflight(self):
        before_status, before = await self.call("bench_one", {"id": 1}, 0, measure=False)
        other_status, other = await self.call("bench_one", {"id": 1}, 1, measure=False)
        if before_status != "http_200" or other_status != "http_200":
            raise RuntimeError(f"preflight fixture/authentication failed ({before_status}, {other_status}); prepare, start and seed the isolated stack")
        status, changed = await self.call("bench_write", {"id": 1}, 0, measure=False)
        if status != "http_200" or changed["version"] != before["version"] + 1:
            raise RuntimeError("preflight update/version check failed")
        _, after = await self.call("bench_one", {"id": 1}, 1, measure=False)
        if after != other:
            raise RuntimeError("preflight tenant isolation failed")
        if self.public_fixture:
            status, _ = await self.call('bench_one', {'id': 1}, measure=False, omit_auth=True)
            if (self.args.auth_mode == 'none' and status != 'http_200') or (self.args.auth_mode == 'jwt' and status not in ('http_401', 'http_403')):
                raise RuntimeError('authentication mode did not match actual endpoint behavior')
            if self.target == 'pgxio':
                status, _ = await self.call('bench_one', {'id': 1}, measure=False, private=True, omit_auth=True)
                if status != 'http_401':
                    raise RuntimeError('benchmark bypass reached an ordinary protected endpoint')
        for jwt in (("invalid", token(self.args.workdir, 0, -60)) if self.args.auth_mode == 'jwt' else ()):
            status, _ = await self.call("bench_one", {"id": 1}, jwt=jwt, measure=False)
            if status not in ("http_401", "http_403"):
                raise RuntimeError("preflight invalid/expired token was accepted")
        if not self.native and self.rest_input_validation == "schema":
            status, _ = await self.call("bench_one", {"id": 0, "extra": True}, measure=False)
            if status != "http_400":
                raise RuntimeError("preflight schema enforcement failed")
        if self.target == 'pgxio' and self.rest_input_validation == 'postgres':
            operation = 'bench_public_one' if self.public_fixture else 'bench_one'
            headers = {'Content-Type': 'application/json'}
            if self.args.auth_mode == 'jwt':
                headers['Authorization'] = 'Bearer ' + self.tokens[0]
            response = await self.client.post(self.url + '/v1/' + operation, content=b'{', headers=headers)
            if response.status_code != 400:
                raise RuntimeError('PostgreSQL-owned JSON validation failed')
        return {"passed": True, "checks": ["read", "update/version",
                'fixture_slice_independence' if self.public_fixture else 'tenant_isolation',
                'JWT_signature/expiry' if self.args.auth_mode == 'jwt' else 'anonymous_fixture_and_protected_endpoint_guard'],
                "schema_checks": "passed" if self.rest_input_validation == "schema" else "SQL_owned" if self.rest_input_validation == "postgres" else "native_gap" if self.native else "unknown", "rest_input_validation": self.rest_input_validation, "output_validation": self.output_validation}

    async def arrivals(self, duration, rate, measure=True):
        if rate <= 0:
            await asyncio.sleep(duration)
            return
        start = time.perf_counter()
        end = start + duration
        active = set()
        index = 0
        # Open model: schedule independently of request completion. Never create
        # unbounded queued tasks; count overload and scheduler misses explicitly.
        while start + index / rate < end:
            scheduled = start + index / rate
            delay = scheduled - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.perf_counter()
            if now >= end:
                if measure:
                    self.metrics.counts["unscheduled"] += math.ceil((end - scheduled) * rate)
                break
            behind = int(max(0, now - scheduled) * rate)
            if behind:
                if measure:
                    self.metrics.counts["unscheduled"] += behind
                index += behind
                scheduled = start + index / rate
            if len(active) >= self.args.max_inflight:
                if measure:
                    self.metrics.counts["dropped_inflight_limit"] += 1
            else:
                operation = {"one": "bench_one", "page": "bench_page", "write": "bench_write",
                             "small-app": "bench_page", "mixed": "bench_page"}[self.args.workload]
                if self.args.workload == "mixed" and self.random.random() < .1:
                    operation = "bench_write"
                tenant = index % self.args.active_tenants
                body = {} if operation == "bench_page" else {"id": 2 if self.args.sockets else 1}
                task = asyncio.create_task(self.call(operation, body, tenant, scheduled, measure=measure))
                active.add(task)
                task.add_done_callback(active.discard)
            index += 1
        # These completions belong to the trial, including drain latency.
        if active:
            await asyncio.gather(*list(active))
        await asyncio.sleep(max(0, end - time.perf_counter()))

    async def socket(self, index):
        tenant = index % self.args.active_tenants
        kwargs = {"max_size": 16384, "max_queue": 16, "open_timeout": self.args.timeout,
                  "ping_interval": 20, "ping_timeout": 20}
        if self.ws_url.startswith("wss:"):
            kwargs["ssl"] = self.ssl
        if self.target != "supabase":
            kwargs["subprotocols"] = ["pgxio.v1"]
        # Self-hosted Realtime restarts its endpoint after tenant bootstrap.
        # Retry only that startup gap, before any timed measurement begins.
        deadline = time.monotonic() + 60
        connect_url = self.ws_url
        if self.target == "supabase":
            connect_url += ("&" if "?" in connect_url else "?") + urlencode({"apikey": self.tokens[tenant]})
        while True:
            try:
                ws = await connect(connect_url, **kwargs)
                break
            except Exception as error:
                status = getattr(getattr(error, "response", None), "status_code", None)
                if self.target != "supabase" or status not in (502, 503) or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(1)
        self.sockets.append(ws)
        topic = "realtime:bench:" + route(tenant)
        if self.target == "supabase":
            await ws.send(json.dumps({"topic": topic, "event": "phx_join", "ref": "1", "payload": {
                "access_token": self.tokens[tenant], "config": {"private": True,
                    "broadcast": {"ack": False, "self": False}, "presence": {"enabled": False},
                    "postgres_changes": []}}}))
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(), self.args.timeout))
                if reply.get("event") == "phx_reply" and reply.get("ref") == "1":
                    if reply["payload"].get("status") != "ok":
                        raise RuntimeError("Supabase private-channel join failed; apply supabase SQL after tenant migration")
                    break
        else:
            await ws.send(json.dumps({"type": "auth", "token": self.tokens[tenant]}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), self.args.timeout))
            if reply.get("type") != "ready":
                raise RuntimeError("WebSocket authentication failed")
            for sub in range(self.args.subscriptions):
                await ws.send(json.dumps({"type": "subscribe", "id": str(sub), "stream": "bench", "params": {}}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), self.args.timeout))
                if reply.get("type") != "subscribed" or reply.get("id") != str(sub):
                    raise RuntimeError("WebSocket subscription failed")
        state = {"index": index, "tenant": tenant, "dirty": False, "refetching": False}
        self.clients.append(state)
        # Subscribe before snapshot: close the initial snapshot race.
        if self.args.realtime_mode != "notify":
            deadline = time.perf_counter() + self.args.timeout
            while True:
                status, _ = await self.call("bench_page", {}, tenant, measure=False)
                if status == "http_200":
                    break
                if status != "http_503" or time.perf_counter() >= deadline:
                    raise RuntimeError("initial subscription snapshot failed: " + status)
                # Setup deliberately tolerates the documented no-queue pool.
                # Timed refetches below do not retry or hide these errors.
                self.setup_snapshot_retries += 1
                await asyncio.sleep(.05)
        self.spawn(self.receive(ws, state))
        if self.target == "supabase":
            self.spawn(self.heartbeat(ws))

    async def heartbeat(self, ws):
        try:
            ref = 100
            while self.running:
                await asyncio.sleep(20)
                await ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": str(ref)}))
                ref += 1
        except Exception:
            if self.running:
                self.metrics.counts["ws_heartbeat_error"] += 1

    async def receive(self, ws, state):
        try:
            while self.running:
                if state["index"] < math.ceil(self.args.sockets * self.args.slow_reader_fraction):
                    await asyncio.sleep(self.args.slow_reader_delay)
                event = json.loads(await ws.recv())
                if self.target == "supabase":
                    if event.get("event") in ("phx_error", "phx_close"):
                        raise RuntimeError("Phoenix channel closed")
                    changed = event.get("event") == "broadcast" and event.get("payload", {}).get("event") == "changed"
                    sid = "0"
                else:
                    changed, sid = event.get("type") == "changed", event.get("id")
                if not changed:
                    continue
                self.metrics.counts["ws_notifications"] += 1
                probe = self.probes.get(state["tenant"])
                key = (state["index"], sid)
                if probe and key in probe.expected:
                    probe.expected.remove(key)
                    self.metrics.notify.add((time.perf_counter() - probe.started) * 1000)
                    if not probe.expected and (self.args.realtime_mode == "notify" or not probe.fresh_expected):
                        probe.done.set()
                if self.args.realtime_mode != "notify":
                    state["dirty"] = True
                    if not state["refetching"]:
                        state["refetching"] = True
                        self.spawn(self.refetch(state))
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.running:
                self.metrics.counts["ws_unexpected_close_or_error"] += 1

    async def refetch(self, state):
        try:
            while state["dirty"] and self.running:
                state["dirty"] = False
                probe = self.probes.get(state["tenant"])
                if probe:
                    await probe.version_ready.wait()
                status, data = await self.call("bench_page", {}, state["tenant"])
                if status != "http_200":
                    self.metrics.counts["snapshot_error"] += 1
                    continue
                if probe and probe.version and state["index"] in probe.fresh_expected:
                    current = next((row["version"] for row in data["rows"] if row["id"] == 1), 0)
                    if current >= probe.version:
                        probe.fresh_expected.remove(state["index"])
                        self.metrics.fresh.add((time.perf_counter() - probe.started) * 1000)
                        if not probe.fresh_expected and not probe.expected:
                            probe.done.set()
                    else:
                        self.metrics.counts["stale_snapshot"] += 1
        finally:
            state["refetching"] = False

    async def probe_loop(self, duration):
        end = time.perf_counter() + duration
        interval = 1 / self.args.probe_rate
        next_at = time.perf_counter()
        index = 0
        while next_at < end and self.running:
            await asyncio.sleep(max(0, next_at - time.perf_counter()))
            if time.perf_counter() >= end:
                break
            tenant = index % self.args.active_tenants
            affected = [s["index"] for s in self.clients if s["tenant"] == tenant]
            expected = {(i, str(sub)) for i in affected for sub in range(self.args.subscriptions)}
            probe = Probe(time.perf_counter(), expected, set(affected))
            self.probes[tenant] = probe
            self.metrics.counts["probes"] += 1
            try:
                status, row = await self.call("bench_write", {"id": 1}, tenant)
                if status == "http_200":
                    probe.version = row["version"]
                else:
                    self.metrics.counts["probe_write_error"] += 1
                probe.version_ready.set()
                await asyncio.wait_for(probe.done.wait(), self.args.event_timeout)
            except asyncio.TimeoutError:
                self.metrics.counts["probe_timeout"] += 1
            finally:
                probe.version_ready.set()
                self.metrics.counts["missing_notifications"] += len(probe.expected)
                if self.args.realtime_mode != "notify":
                    self.metrics.counts["missing_fresh_snapshots"] += len(probe.fresh_expected)
                self.probes.pop(tenant, None)
            index += 1
            next_at += interval
            if next_at < time.perf_counter():
                skipped = int((time.perf_counter() - next_at) / interval)
                self.metrics.counts["probe_unscheduled"] += skipped
                next_at += skipped * interval

    async def close(self):
        self.running = False
        for probe in self.probes.values():
            probe.version_ready.set()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await asyncio.gather(*(ws.close() for ws in self.sockets), return_exceptions=True)
        await self.client.aclose()


def valid_response(operation, value):
    def row(item):
        return (isinstance(item, dict) and set(item) == {"id", "version", "payload"}
                and type(item["id"]) is int and item["id"] >= 1
                and type(item["version"]) is int and item["version"] >= 1
                and isinstance(item["payload"], str))
    if operation == "bench_page":
        return (isinstance(value, dict) and set(value) == {"rows"} and isinstance(value["rows"], list)
                and len(value["rows"]) == 100 and all(row(item) for item in value["rows"]))
    return row(value)


async def run(args):
    driver = Driver(args)
    if args.sockets and driver.target == "postgrest":
        await driver.close()
        raise ValueError("standalone PostgREST has no client realtime service")
    if driver.target == "supabase" and args.subscriptions != 1:
        await driver.close()
        raise ValueError("Supabase native adapter uses one private topic/socket; 16-subscription parity is unsupported")
    if args.workload == "mixed" and args.sockets:
        await driver.close()
        raise ValueError("use page/one background load with sockets; mixed writes cannot causally identify opaque invalidations")
    report = {"schema_version": 1, "kind": "competitive_benchmark", "target": driver.target,
              "purpose": getattr(args, "purpose", "measurement"),
              "started_unix": time.time(), "load_generator": {"platform": platform.platform(),
                  "python": platform.python_version()}, "configuration": public_settings(driver.settings),
              "workload": {k: getattr(args, k) for k in ("workload", "rate", "duration", "warmup", "sockets",
                  "subscriptions", "active_tenants", "realtime_mode", "probe_rate", "max_inflight", "seed",
                  "slow_reader_fraction", "slow_reader_delay")},
              "transport": "https/wss" if args.url.startswith("https:") else "http/ws",
              "engine": args.engine,
              "rest_input_validation": driver.rest_input_validation, "output_validation": driver.output_validation,
              "authentication": {'mode': args.auth_mode, 'fixture': args.auth_fixture,
                  'jwt_cache': driver.settings.get('jwt_cache', 'default')},
              "latency_definition": "write request start to notification or version-verified snapshot on the same client clock",
              "notification_semantics": "cached_channel_authorization" if driver.target == "supabase" else "per_invalidation_reauthorization",
              "measurement_status": "load_only; attach Linux collector evidence for RAM/CPU claims"}
    lockfile = Path(args.workdir) / "images.lock.json"
    if lockfile.exists():
        report["image_lock"] = json.loads(lockfile.read_text())
    report["workload"]["request_bytes"] = getattr(args, "request_bytes", 0)
    rate = args.rate
    if args.workload == "small-app":
        if args.sockets != 100 or args.active_tenants != 10 or args.subscriptions != 1 or args.probe_rate != 1:
            await driver.close()
            raise ValueError("small-app requires 100 sockets, 10 active tenants, 1 subscription and 1 probe/sec")
        rate -= args.probe_rate * (1 + (args.sockets / args.active_tenants if args.realtime_mode != "notify" else 0))
        if rate < 0:
            await driver.close()
            raise ValueError("total rate cannot cover writes and expected snapshot refetch traffic")
    report["workload"]["background_rate"] = rate
    started = None
    try:
        report["preflight"] = await driver.preflight()
        if args.engine in ("wrk", "wrk2"):
            from .wrk import run_rest
            started = time.perf_counter()
            await run_rest(driver, report)
            report["completed"] = True
            return report
        await driver.arrivals(args.warmup, rate, measure=False)
        # Bound handshake concurrency to avoid making setup a reconnect-storm test.
        semaphore = asyncio.Semaphore(min(8, args.max_inflight))
        async def establish(index):
            async with semaphore:
                await driver.socket(index)
        await asyncio.gather(*(establish(i) for i in range(args.sockets)))
        # A settling window keeps setup notifications and initial snapshots out of metrics.
        if args.sockets:
            await asyncio.sleep(.2)
        driver.metrics = Metrics()
        report["setup"] = {"initial_snapshot_503_retries": driver.setup_snapshot_retries}
        started = time.perf_counter()
        report["measurement_started_unix"] = time.time()
        tasks = [asyncio.create_task(driver.arrivals(args.duration, rate))]
        if args.sockets:
            tasks.append(asyncio.create_task(driver.probe_loop(args.duration)))
        await asyncio.gather(*tasks)
        # Allow refetches already caused by in-window notifications to finish.
        deadline = time.perf_counter() + args.timeout
        while any(s["refetching"] for s in driver.clients) and time.perf_counter() < deadline:
            await asyncio.sleep(.01)
        report["completed"] = True
    except BaseException as error:
        report["completed"] = False
        # Do not emit URLs, credentials, response bodies, or JWTs in diagnostics.
        report["failure_type"] = type(error).__name__
        raise
    finally:
        elapsed = time.perf_counter() - started if started else 0
        if args.engine in ("wrk", "wrk2") and "measured_elapsed_seconds" in report:
            elapsed = report["measured_elapsed_seconds"]
        report.setdefault("measurement_finished_unix", time.time())
        report["measured_elapsed_seconds"] = elapsed
        report["elapsed_seconds_including_drain"] = elapsed
        if args.engine == "python" or "results" not in report:
            report["results"] = driver.metrics.summary(elapsed)
        await driver.close()
        if report.get("completed") and args.workload == "small-app":
            # RAM qualification includes recovery at the same enforced cap.
            # Keep this check outside the steady-state latency/CPU interval.
            recovery = Driver(args)
            try:
                semaphore = asyncio.Semaphore(min(8, args.max_inflight))
                async def reconnect(index):
                    async with semaphore:
                        await recovery.socket(index)
                await asyncio.gather(*(reconnect(i) for i in range(args.sockets)))
                await recovery.probe_loop(.1)
                counts = recovery.metrics.counts
                report["reconnect_verified"] = (recovery.metrics.fresh.count > 0 and
                    not any(counts.get(k, 0) for k in ("probe_timeout", "missing_notifications", "missing_fresh_snapshots")))
            except Exception as error:
                report["reconnect_verified"] = False
                report["reconnect_failure_type"] = type(error).__name__
            finally:
                await recovery.close()
        report["finished_unix"] = time.time()
        dump(args.output, report)
    return report

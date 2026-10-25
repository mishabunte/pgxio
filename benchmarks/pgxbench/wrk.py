"""Use wrk/wrk2 for REST; Python remains the correctness/realtime control plane."""
import asyncio
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from .common import ROOT, dump, subject
from .load import Histogram


def lua_string(value):
    # Config values are UTF-8 strings; use Lua's decimal byte escapes, not JSON
    # \u escapes (which Lua 5.1 does not understand). No executable input crosses.
    return '"' + ''.join('\\%03d' % byte for byte in value.encode()) + '"'


def config_text(driver):
    result = ['return {tokens={', ','.join(lua_string(t) for t in driver.tokens[:driver.args.active_tenants]), '},']
    result += ['native=', 'true' if driver.native else 'false', ',prefix=',
               lua_string('/rpc/' if driver.native else '/v1/'), ',workload=', lua_string(driver.args.workload)]
    result += [',auth_mode=', lua_string(driver.args.auth_mode), ',public_fixture=',
               'true' if driver.public_fixture else 'false', ',subjects={',
               ','.join(lua_string(subject(i)) for i in range(driver.args.active_tenants)), '}']
    if driver.args.api_key_env:
        result += [',api_key=', lua_string(os.environ[driver.args.api_key_env])]
    result += [',request_bytes=', str(getattr(driver.args, 'request_bytes', 0))]
    result.append('}\n')
    return ''.join(result)


def parse_output(stdout):
    matches = [line[len('PGXBENCH_JSON '):] for line in stdout.splitlines() if line.startswith('PGXBENCH_JSON ')]
    if len(matches) != 1:
        raise RuntimeError("wrk did not produce its structured result; verify binary and Lua script compatibility")
    value = json.loads(matches[0])
    if sum(value['statuses'].values()) != value['requests']:
        raise RuntimeError(f"wrk response-status accounting did not match completed requests: statuses={sum(value['statuses'].values())}, requests={value['requests']}")
    return value


async def invoke(driver, duration):
    args = driver.args
    engine = args.engine
    if duration < 20 and engine == 'wrk2':
        raise ValueError("wrk2 trials/warmups must be at least 20 seconds to cover its 10-second calibration")
    if args.connections < args.threads or args.connections % args.threads:
        raise ValueError("connections must be divisible by load-generator threads")
    if engine == 'wrk2' and (args.rate < args.connections or args.rate != int(args.rate)):
        raise ValueError("wrk2 requires an integer rate >= connections; reduce --connections for low-rate tests")
    config = config_text(driver)
    with tempfile.TemporaryDirectory(prefix='pgxbench-wrk-', dir=args.workdir) as temporary:
        folder = Path(temporary)
        private = folder / 'config.lua'
        descriptor = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(config)
        script = ROOT / 'wrk' / 'workload.lua'
        binary = args.wrk_binary or engine
        container_name = None
        env = dict(os.environ, PGXBENCH_WRK_CONFIG=str(private))
        if args.wrk_image:
            container_name = 'pgxbench-load-' + uuid.uuid4().hex[:16]
            inspect = await asyncio.create_subprocess_exec('docker', 'image', 'inspect', args.wrk_image, '--format', '{{json .}}',
                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            image_id, _ = await inspect.communicate()
            if inspect.returncode:
                raise RuntimeError('build the wrk image before running: docker build -t pgxbench-wrk benchmarks/wrk')
            argv = ['docker', 'run', '--rm', '--name', container_name, '--network', 'host',
                    '-v', str(folder.resolve()) + ':/config:ro', '-v', str(script.resolve()) + ':/workload.lua:ro',
                    '-e', 'PGXBENCH_WRK_CONFIG=/config/config.lua', args.wrk_image, binary]
            if getattr(args, 'wrk_cpuset', None):
                argv[2:2] = ['--cpuset-cpus', args.wrk_cpuset]
            script_path = '/workload.lua'
            image_metadata = json.loads(image_id)
            identity = {"image": args.wrk_image, "image_id": image_metadata['Id'],
                        "clock_source": (image_metadata.get('Config', {}).get('Labels') or {}).get('io.pgxbench.clock', 'unknown')}
        else:
            resolved = shutil.which(binary)
            if not resolved:
                raise RuntimeError(f"{binary} not found; install it on the load generator or use --wrk-image pgxbench-wrk")
            argv = [resolved]
            script_path = str(script)
            identity = {"binary_sha256": hashlib.sha256(Path(resolved).read_bytes()).hexdigest()}
        argv += ['-t', str(args.threads), '-c', str(args.connections), '-d', f'{math.ceil(duration)}s',
                 '--timeout', f'{math.ceil(args.timeout)}s', '--latency', '-s', script_path]
        if engine == 'wrk2':
            argv += ['-R', str(int(args.rate))]
        argv.append(driver.url)
        process = await asyncio.create_subprocess_exec(*argv, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), duration + args.timeout + 60)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if container_name:
                cleanup = await asyncio.create_subprocess_exec('docker', 'rm', '--force', container_name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await cleanup.wait()
            raise
        if process.returncode:
            dump(Path(args.output).with_suffix('.wrk-diagnostic.json'),
                 {'exit_code': process.returncode, 'stdout': stdout.decode(), 'stderr': stderr.decode()}, private=True)
            raise RuntimeError("wrk failed; check the binary/image, URL, resource limits and TLS support")
        try:
            result = parse_output(stdout.decode())
        except Exception:
            dump(Path(args.output).with_suffix('.wrk-diagnostic.json'),
                 {'stdout': stdout.decode(), 'stderr': stderr.decode()}, private=True)
            raise
        result['engine_identity'] = identity
        # Raw output contains no tokens (only the public target URL and timing).
        result['raw_output'] = stdout.decode()
        result['stderr'] = stderr.decode()
        return result


async def run_rest(driver, report):
    args = driver.args
    if args.warmup:
        await invoke(driver, args.warmup)
    report['measurement_started_unix'] = time.time()
    start = time.perf_counter()
    result = await invoke(driver, args.duration)
    elapsed = time.perf_counter() - start
    report['measurement_finished_unix'] = time.time()
    report['measured_elapsed_seconds'] = elapsed
    counts = {"http_" + status: count for status, count in result['statuses'].items()}
    counts.update({"transport_" + name: count for name, count in result['errors'].items()
                   if name != 'status' and count})
    offered = int(args.rate * result['duration_seconds']) if args.engine == 'wrk2' else None
    completed = result['requests']
    if offered is not None:
        counts['offered_minus_completed'] = max(0, offered - completed)
    empty = Histogram().summary()
    report['engine'] = args.engine
    report['load_model'] = 'constant_rate_corrected_latency' if args.engine == 'wrk2' else 'closed_loop_maximum_throughput'
    report['wrk'] = {**result, 'threads': args.threads, 'connections': args.connections,
                     'TLS_certificate_validation': 'verified_preflight; wrk does not verify peer certificates',
                     'response_validation': 'full preflight and postflight; status accounting during load',
                     'latency_population': 'wrk engine native histogram; raw output retained',
                     'latency_calibration_seconds': 10 if args.engine == 'wrk2' else .5}
    report['results'] = {'counts': counts, 'operations': {args.workload: completed},
        'successful_requests_per_second': counts.get('http_200', 0) / result['duration_seconds'],
        'http_latency': result['latency_ms'], 'successful_http_latency': None,
        'scheduler_delay': empty, 'notification_latency': empty, 'fresh_data_latency': empty}
    # Verify tenant/response/auth invariants again after the timed run.
    report['postflight'], report['postflight_retries'] = await postflight(driver)
    return elapsed


async def postflight(driver):
    # Closing load-generator sockets can discard active database connections.
    # Pool recovery happens outside the measured interval; timed 503s stay errors.
    deadline = time.monotonic() + driver.args.timeout
    retries = 0
    while True:
        try:
            return await driver.preflight(), retries
        except RuntimeError as error:
            message = str(error)
            if (not message.startswith('preflight fixture/authentication failed')
                    or 'http_503' not in message
                    or any(status not in ('http_200', 'http_503') for status in
                           re.findall(r'\b(?:http_\d+|transport_\w+|invalid_response)\b', message))
                    or time.monotonic() >= deadline):
                raise
            retries += 1
            await asyncio.sleep(.1)

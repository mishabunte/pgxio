"""Paired REST authentication experiments in the isolated benchmark project."""
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import random
import time

from .common import ROOT, command, compose, dump, load_settings
from .campaign import run_namespace, wait_ready
from .load import Driver, run
from .operator import stats
from .report import make_report
from .setup import render, seed, lock


def jobs(args):
    targets = args.targets.split(',')
    if not targets or any(t not in ('pgxio', 'postgrest') for t in targets):
        raise ValueError('paired authentication comparison currently supports pgxio/postgrest')
    if args.repetitions < 1 or args.warmup < 0:
        raise ValueError('repetitions must be positive and warmup nonnegative')
    result = []
    for target in targets:
        profiles = [('jwt', 'off'), ('none', 'off')]
        if target == 'postgrest':
            profiles.append(('jwt', 'default'))
        for mode, cache in profiles:
            for connections in args.connections:
                for repetition in range(1, args.repetitions + 1):
                    result.append(dict(target=target, auth_mode=mode, jwt_cache=cache, auth_fixture='public',
                        workload='page', rate=0, sockets=0, subscriptions=1, active_tenants=10, probe_rate=1,
                        realtime_mode='both', engine='wrk', connections=connections,
                        threads=1 if connections == 1 else 2 if connections % 2 == 0 else 1,
                        wrk_image=args.wrk_image, wrk_cpuset=args.wrk_cpuset,
                        duration=args.duration, warmup=args.warmup, repetition=repetition))
    random.Random(args.seed).shuffle(result)
    for index, job in enumerate(result):
        job['id'] = f'{index:03d}'
    return result


async def fixture_ready(config):
    deadline = time.monotonic() + 30
    while True:
        driver = Driver(config)
        try:
            return await driver.preflight()
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(.5)
        finally:
            await driver.close()


async def compare(args, specification=None):
    specification = jobs(args) if specification is None else specification
    if not args.execute:
        return {'dry_run': True, 'jobs': len(specification), 'trials': specification,
                'minimum_load_minutes': sum(j['duration'] + j['warmup'] for j in specification) / 60}
    if any(job.get('database_delay_ms',0)>0 for job in specification) and platform.system() != 'Linux':
        raise RuntimeError('database delay experiments must run as an operator on the Linux Docker host')
    output = Path(args.output)
    if (output / 'campaign.json').exists():
        raise FileExistsError('use a fresh authentication comparison output directory')
    output.mkdir(parents=True, exist_ok=True)
    dump(output / 'campaign.json', {'kind': 'performance_stages' if any('profile' in job for job in specification) else 'paired_authentication', 'seed': args.seed, 'jobs': specification})
    original = {'auth_mode': 'jwt', 'auth_fixture': 'tenant', 'jwt_cache': 'default',
                'pgxio_image': '', 'module_options': {}, 'contract_enabled': True, 'config_format': 'auto', 'nginx_profile': 'baseline', 'performance_profile': '', 'database_egress_delay_ms': 0,
                **load_settings(args.workdir)}
    if not (Path(args.workdir) / 'compose.json').exists():
        render(args.workdir, original)
    docker = json.loads(await asyncio.to_thread(command, ['docker', 'info', '--format', '{{json .}}']))
    environment = {'kind': 'local_performance_comparison' if any('profile' in job for job in specification) else 'local_authentication_comparison', 'started_unix': time.time(),
        'host': platform.platform(), 'docker': {k: docker.get(k) for k in
            ('Architecture', 'NCPU', 'MemTotal', 'KernelVersion', 'ServerVersion', 'ContainersRunning')},
        'limitations': ['Shared local host; exploratory repetitions, not a production ranking',
                        'Closed-loop wrk load; no minimum RAM or fixed-rate capacity claim'],
        'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in ROOT.rglob('*') if p.is_file() and not {'__pycache__', 'target', 'node_modules', '.git'}.intersection(p.parts)}}
    dump(output / 'environment.json', environment)
    outcomes, built = [], set()
    try:
        for job in specification:
            stem = (job.get('profile', '') + '-' if job.get('profile') else '') + f"{job['id']}-{job['target']}-{job['auth_mode']}-{job['jwt_cache']}-{job['connections']}c-r{job['repetition']}"
            path = output / 'results' / (stem + '.json')
            print(f"START {stem} ({len(outcomes)+1}/{len(specification)})", flush=True)
            outcome = {'job': job, 'passed': False}
            try:
                await asyncio.to_thread(compose, args.workdir, 'down', '--remove-orphans')
                render(args.workdir, dict(target=job['target'], frontend='http', auth_fixture='public',
                    database_tls=job.get('database_tls', 'verify-full'),
                    contract_enabled=job.get('contract_enabled', True), config_format=job.get('config_format', 'auto'),
                    auth_mode=job['auth_mode'], jwt_cache=job['jwt_cache'],
                    cpus=job.get('cpus', 1), workers=job.get('workers', 1),
                    pgxio_image=job.get('pgxio_image', getattr(args, 'pgxio_image', '') or ''),
                    module_options=job.get('module_options', getattr(args, 'module_options', {}) or {}),
                    database_egress_delay_ms=0, nginx_profile=job.get('nginx_profile', 'baseline'), performance_profile=job.get('profile', ''),
                    pool_mode='fixed', memory_mib=0, db_memory_mib=0, cgroup_parent='', cgroup_path='',
                    cpuset=args.api_cpuset or '', db_cpuset=args.db_cpuset or ''))
                if job['target'] == 'pgxio' and 'pgxio' not in built and not job.get('pgxio_image', getattr(args, 'pgxio_image', None)):
                    await asyncio.to_thread(compose, args.workdir, 'build', 'pgxio')
                    built.add('pgxio')
                await asyncio.to_thread(compose, args.workdir, 'up', '--detach', '--remove-orphans')
                await wait_ready(args.workdir, args.url, 120)
                await asyncio.to_thread(seed, args.workdir, 10000, 100, 128)
                await asyncio.to_thread(lock, args.workdir)
                config = run_namespace(args.workdir, path, job, args.url)
                if args.duration < 10:
                    config.purpose = 'functional'
                await fixture_ready(config)
                if job.get('database_delay_ms',0):
                    from .operator import network_delay
                    await asyncio.to_thread(network_delay,args.workdir,job['database_delay_ms'])
                await asyncio.to_thread(stats, args.workdir, output / 'db' / (stem + '-before.json'))
                if 'database_tls' in job:
                    observation = json.loads((output / 'db' / (stem + '-before.json')).read_text())['postgres']
                    sessions = [s for s in observation['connections'] if s['user'] == 'web']
                    encrypted = job['database_tls'] != 'disable'
                    if not sessions or any(s.get('ssl') is not encrypted for s in sessions):
                        raise RuntimeError('database connections do not match requested TLS mode')
                collector = None
                stop = asyncio.Event()
                host = getattr(args, 'collector_host', 'none')
                if host != 'none':
                    from .resources import collect, collect_colima
                    resource_path = path.with_suffix('.resources.jsonl')
                    if host == 'colima':
                        ready = asyncio.Event()
                        collector = asyncio.create_task(collect_colima(args.workdir, resource_path, stop, ready))
                    else:
                        collector = asyncio.create_task(collect(args.workdir, resource_path, stop=stop))
                try:
                    if collector and host == 'colima':
                        waiter = asyncio.create_task(ready.wait())
                        done, _ = await asyncio.wait([waiter, collector], timeout=15,
                            return_when=asyncio.FIRST_COMPLETED)
                        if collector in done:
                            await collector
                        if not ready.is_set():
                            waiter.cancel()
                            raise RuntimeError('resource sampler did not become ready')
                    trial = await run(config)
                finally:
                    stop.set()
                    if collector:
                        await asyncio.wait_for(collector, 10)
                await asyncio.to_thread(stats, args.workdir, output / 'db' / (stem + '-after.json'))
                if host != 'none':
                    from .resources import summarize
                    evidence = summarize(resource_path, trial)
                    if not evidence['valid']:
                        raise RuntimeError('invalid resource evidence: ' + ','.join(evidence['errors']))
                    outcome['resources'] = evidence['averages']
                counts = trial['results']['counts']
                if not counts.get('http_200'):
                    raise RuntimeError('no successful requests in authentication trial')
                outcome.update(passed=True, successful_rps=trial['results']['successful_requests_per_second'], counts=counts)
                print(json.dumps({k:v for k,v in outcome.items() if k != 'job'}), flush=True)
            except Exception as error:
                outcome.update(failure_type=type(error).__name__, detail=str(error))
                print(json.dumps({k:v for k,v in outcome.items() if k != 'job'}), flush=True)
                # Stop on a tool/configuration failure; retain evidence and clean up.
                outcomes.append(outcome)
                break
            outcomes.append(outcome)
            make_report(output / 'results', output / 'report', include_functional=True)
            dump(output / 'outcomes.json', outcomes)
    finally:
        await asyncio.to_thread(compose, args.workdir, 'down', '--remove-orphans')
        render(args.workdir, original)
        environment['finished_unix'] = time.time()
        dump(output / 'environment.json', environment)
        dump(output / 'outcomes.json', outcomes)
        make_report(output / 'results', output / 'report', include_functional=True)
    return {'passed': len(outcomes) == len(specification) and all(o['passed'] for o in outcomes),
            'completed_trials': len(outcomes), 'expected_trials': len(specification), 'output': str(output)}

"""Direct SQL reference; run separately from HTTP traffic, never concurrently."""
import asyncio
import csv
import json
from pathlib import Path
import random
import re
import time

from .common import command, compose, dump, load_settings, public_settings
from .setup import render, seed, lock
from .campaign import wait_ready
from .operator import stats
from .resources import collect_colima, summarize


def script(workload):
    if workload not in ('one', 'page', 'write', 'mixed'):
        raise ValueError('unknown SQL workload')
    function = 'page' if workload == 'mixed' else workload
    statement = lambda name: ("SELECT web.bench_public_" + name + "(jsonb_build_object('tenant',"
        "lpad(to_hex(:tenant::bigint),32,'0')::uuid::text,'id',1));\n")
    body = statement(function)
    if workload == 'mixed':
        body = '\\set operation random(1,100)\n\\if :operation <= 10\n' + statement('write') + '\\else\n' + statement('page') + '\\endif\n'
    return ("\\set tenant random(1,10)\nBEGIN;\n"
        "SELECT jsonb_build_object('claims',set_config('request.jwt.claims','{}',true),"
        "'timeout',set_config('statement_timeout','4000',true),"
        "'lock',set_config('lock_timeout','1000',true),"
        "'path',set_config('search_path','pg_catalog,web',true));\n" + body + 'COMMIT;\n')


def parse(text):
    def value(pattern):
        match = re.search(pattern, text)
        if not match:
            raise RuntimeError('missing pgbench result: ' + pattern)
        return float(match[1])
    return {'transactions': int(value(r'number of transactions actually processed:\s*(\d+)')),
        'failed_transactions': int(value(r'number of failed transactions:\s*(\d+)')),
        'tps': value(r'tps = ([\d.]+) \(without initial connection time\)'),
        'latency_mean_ms': value(r'latency average = ([\d.]+) ms')}


async def baseline(args):
    original = load_settings(args.workdir)
    if original.get('external_database'):
        raise ValueError('this pgbench adapter currently requires the dedicated local benchmark database')
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError('use a fresh pgbench output directory')
    output.mkdir(parents=True)
    jobs = [(c, r) for c in args.connections for r in range(1, args.repetitions + 1)]
    random.Random(20261015).shuffle(jobs)
    rows = []
    try:
        await asyncio.to_thread(compose, args.workdir, 'down', '--remove-orphans')
        settings = render(args.workdir, dict(target='pgxio', frontend='http', auth_fixture='public', auth_mode='jwt',
            pgxio_image=args.image, module_options={}, memory_mib=0, db_memory_mib=0,
            cpus=1, workers=1, pool_mode='fixed', cpuset=args.api_cpuset, db_cpuset=args.db_cpuset))
        await asyncio.to_thread(compose, args.workdir, 'up', '--detach', '--remove-orphans')
        await wait_ready(args.workdir, 'http://localhost:18080', 120)
        await asyncio.to_thread(seed, args.workdir, 10000, 100, 128)
        await asyncio.to_thread(lock, args.workdir)
        (output / 'workload.sql').write_text(script(args.workload))
        # Docker reads this private file; passwords never appear in argv or public reports.
        credentials = output / 'pgbench.env'
        credentials.touch(mode=0o600)
        credentials.write_text('PGPASSWORD=' + settings['web_password'] + '\n')
        for index, (clients, repetition) in enumerate(jobs):
            stem = f'{index:03d}-{args.workload}-{clients}c-r{repetition}'
            print('START pgbench ' + stem, flush=True)
            prefix = ['docker', 'run', '--rm', '--network', 'pgxbench_bench', '--cpuset-cpus', args.generator_cpuset,
                '--env-file', str(credentials), '-e', 'PGHOST=postgres', '-e', 'PGHOSTADDR=172.29.88.2',
                '-e', 'PGUSER=web', '-e', 'PGDATABASE=pgxbench', '-e', 'PGSSLMODE=verify-full',
                '-e', 'PGSSLROOTCERT=/bench/database-ca.pem',
                '-v', str(Path(args.workdir).resolve() / 'runtime') + ':/bench:ro',
                '-v', str(output / 'workload.sql') + ':/workload.sql:ro', settings['versions']['postgres'],
                'pgbench', '-n', '-M', 'prepared', '-c', str(clients), '-j', str(min(2, clients)),
                '-f', '/workload.sql']
            if args.warmup:
                await asyncio.to_thread(command, prefix + ['-T', str(args.warmup)])
            await asyncio.to_thread(stats, args.workdir, output / (stem + '-db-before.json'))
            stop, ready = asyncio.Event(), asyncio.Event()
            resource_path = output / (stem + '.resources.jsonl')
            collector = asyncio.create_task(collect_colima(args.workdir, resource_path, stop, ready))
            waiter = asyncio.create_task(ready.wait())
            report = {'kind': 'direct_sql_pgbench', 'target': 'pgxio', 'configuration': public_settings(settings),
                'results': {'counts': {}}, 'started_unix': time.time()}
            try:
                done, _ = await asyncio.wait([collector, waiter], timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if collector in done:
                    await collector
                if not ready.is_set():
                    raise RuntimeError('pgbench resource sampler not ready')
                report['measurement_started_unix'] = time.time()
                raw = await asyncio.to_thread(command, prefix + ['-T', str(args.duration), '-r'])
                report['measurement_finished_unix'] = time.time()
            finally:
                stop.set()
                waiter.cancel()
                await asyncio.wait_for(collector, 10)
            report['finished_unix'] = time.time()
            await asyncio.to_thread(stats, args.workdir, output / (stem + '-db-after.json'))
            result = parse(raw)
            evidence = summarize(resource_path, report)
            if not evidence['valid'] or result['failed_transactions']:
                raise RuntimeError('invalid pgbench trial: ' + str(evidence['errors']))
            (output / (stem + '.txt')).write_text(raw)
            row = {'connections': clients, 'repetition': repetition, **result,
                'db_mean_pss_mib': evidence['averages']['database_pss_bytes'] / 1048576,
                'db_mean_cpu_percent': evidence['averages']['database_cpu_percent'],
                'host_load1_mean': evidence['averages']['host_load1']}
            rows.append(row)
            dump(output / (stem + '.json'), {**report, 'pgbench': result, 'resources': evidence,
                'image_lock': json.loads((Path(args.workdir) / 'images.lock.json').read_text())})
            dump(output / 'summary.json', {'rows': rows, 'workload': args.workload,
                'note': 'Direct prepared SQL, sequential BEGIN/settings/function/COMMIT, no HTTP/JWT/schema validation. '
                        'Public fixture uses empty claims. Counts above eight exceed the API query-pool size. '
                        'API container is idle. This baseline is not a product throughput result.'})
            with (output / 'summary.csv').open('w') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row))
                writer.writeheader()
                writer.writerows(rows)
            print(json.dumps(row), flush=True)
    finally:
        await asyncio.to_thread(compose, args.workdir, 'down', '--remove-orphans')
        render(args.workdir, original)
    return {'completed_trials': len(rows), 'output': str(output)}

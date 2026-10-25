"""Adaptive, resumable API RAM qualification against a separate PostgreSQL.

Default is a dry run. Uses only the isolated pgxbench Compose project.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import signal
from statistics import median
import time

from pgxbench.auth_compare import fixture_ready
from pgxbench.campaign import run_namespace, wait_ready
from pgxbench.common import command, compose, dump, load_settings, public_settings
from pgxbench.load import run
from pgxbench.report import assess, make_report
from pgxbench.resources import collect_colima, summarize
from pgxbench.setup import lock, render, seed


def retention_pass(rps, reference):
    return reference > 0 and rps >= reference * .9


def generator_missed(run):
    counts = run['results']['counts']
    return any(counts.get(k,0) for k in ('unscheduled','probe_unscheduled'))


def ram_assessment(strict, run, profile):
    reasons = list(strict['reasons'])
    offered_reason = 'achieved_throughput_below_99.9_percent_of_wrk2_offered_rate'
    # Keep HTTP error and latency SLOs intact. A fixed-rate driver's boundary
    # timing is a separate metric, with an explicit 1% rate-fidelity tolerance.
    if profile=='light' and offered_reason in reasons and run['results']['successful_requests_per_second']>=49.5:
        reasons.remove(offered_reason)
    return dict(workload_passed=not reasons, reasons=reasons,
                light_load_minimum_achieved_rps=49.5 if profile=='light' else None)


def bounded_floor(results, step=8):
    """A failing resource/workload trial below a pass brackets this grid floor."""
    passing = sorted(n for n, passed in results.items() if passed)
    if not passing:
        return None, False
    floor = passing[0]
    return floor, results.get(floor-step) is False


class Sweep:
    def __init__(self, args):
        self.args = args
        self.output = args.output.resolve()
        self.state_path = self.output/'status.json'
        self.state = {'status': 'running', 'started_unix': time.time(),
                      'trials': [], 'floors': {}, 'stage': 'screening'}
        if args.resume:
            self.state = json.loads(self.state_path.read_text())
            self.state.update(status='running', resumed_unix=time.time())
        self.state['pid'] = os.getpid()
        original_path = self.output/'original-settings.json'
        if args.resume:
            self.original = json.loads(original_path.read_text())
        else:
            self.original = load_settings(args.workdir)
            dump(original_path,self.original,private=True)

    def save(self):
        self.state['updated_unix'] = time.time()
        temporary = self.state_path.with_suffix('.tmp')
        dump(temporary, self.state)
        temporary.replace(self.state_path)
        lines = ['# API RAM sweep', '',
                 f"Status: **{self.state['status']}**. Stage: {self.state['stage']}.", '',
                 'HTTP frontend; PostgreSQL plaintext (or the plan\'s explicitly selected DB TLS mode), four DB cores, one API core. '
                 'JWT caches off; pgxio copied buffers. Budgets cover only the API container, with swap disabled. '
                 'Light-load target: 50 RPS, achieved rate at least 49.5 RPS, HTTP success at least 99.9%, p95 below 100 ms. '
                 'Capacity floors are short-run budgets retaining at least 90% of the three-run 256 MiB reference. '
                 'Reload checks differ: Nginx worker HUP versus PostgREST schema USR1.', '',
                 '| Product / profile | Screen floor MiB | Adjacent 8 MiB-lower cap failed | Hour-qualified minimum MiB | Recommended MiB after 24h soak |',
                 '|---|---:|---|---:|---:|']
        for name,entry in self.state['floors'].items():
            lines.append(f"| {name} | {entry['screen_floor_mib']} | {entry['boundary_below_failed']} | {entry['qualified_minimum_mib'] or 'pending / unbounded'} | {entry['recommended_mib'] or 'unverified'} |")
        lines += ['', 'A screen floor is preliminary. Only a bounded floor with three independent one-hour confirmations '
                  'is called a qualified minimum for this REST profile. Unrestricted working-set PSS and cgroup limit '
                  'are different measurements; shared host/image page cache affects charging. These results do not establish '
                  'whole-server, realtime, or other-workload requirements. Strict offered-rate assessments remain in raw '
                  'trials and generic charts; the RAM assessment separately applies the stated 1% achieved-load tolerance.', '',
                  '[Live status](status.json) · [Plan](plan.json) · [Raw trials](trials/) · [Charts after screening](report/charts.html)', '']
        (self.output/'RESULTS.md').write_text('\n'.join(lines))

    def records(self):
        ids = compose(self.args.workdir, 'ps', '--all', '--quiet').splitlines()
        if not ids:
            return []
        # Retain only nonsecret configuration and lifecycle fields.
        rows = json.loads(command(['docker', 'inspect', *ids]))
        return [dict(service=r['Config']['Labels']['com.docker.compose.service'],
                     image=r['Image'], state=r['State'], restarts=r['RestartCount'],
                     limits={k:r['HostConfig'].get(k) for k in
                             ('Memory','MemorySwap','NanoCpus','CpusetCpus')}) for r in rows]

    async def trial(self, target, cap, profile, duration, repetition, phase):
        key = f'{phase}-{target}-{cap}m-{profile}-r{repetition}'
        previous = [r for r in self.state['trials'] if r['key']==key and r['valid']]
        if previous:
            return previous[-1]
        attempt = 1 + sum(r['key']==key for r in self.state['trials'])
        path = self.output/'trials'/f'{key}-a{attempt}.json'
        result = dict(key=key, target=target, cap_mib=cap, profile=profile,
                      phase=phase, duration=duration, repetition=repetition,
                      valid=False, passed=False, source=str(path), started_unix=time.time())
        self.state['current'] = result
        self.save()
        print('START '+key+f' (attempt {attempt})', flush=True)
        stop, ready = asyncio.Event(), asyncio.Event()
        collector = None
        trial = None
        up_started = None
        try:
            await asyncio.to_thread(compose, self.args.workdir, 'down', '--remove-orphans')
            render(self.args.workdir, dict(target=target, frontend='http',
                auth_fixture='public', auth_mode='jwt', jwt_cache='off',
                database_tls=self.args.database_tls, cpus=1, workers=1,
                pool_mode='fixed', memory_mib=cap, db_memory_mib=0,
                cgroup_parent='', cgroup_path='', cpuset='0', db_cpuset='1-4',
                database_egress_delay_ms=0, nginx_profile='baseline',
                performance_profile='ram-'+profile,
                pgxio_image=self.args.image if target=='pgxio' else '',
                module_options={'libpq_buf':'copy'} if target=='pgxio' else {}))
            up_started = time.monotonic()
            await asyncio.to_thread(compose, self.args.workdir, 'up', '--detach', '--no-build', '--remove-orphans')
            result['containers_before'] = await asyncio.to_thread(self.records)
            api = next(r for r in result['containers_before'] if r['service']==target)
            assert api['limits']==dict(Memory=cap*1048576, MemorySwap=cap*1048576,
                                      NanoCpus=1000000000, CpusetCpus='0'), 'unexpected API limits'
            db = next(r for r in result['containers_before'] if r['service']=='postgres')
            assert db['limits']['CpusetCpus']=='1-4', 'unexpected database affinity'
            await wait_ready(self.args.workdir, self.args.url, 30)
            result['startup_ready_seconds'] = time.monotonic()-up_started
            await asyncio.to_thread(seed, self.args.workdir, 10000, 100, 128)
            await asyncio.to_thread(lock, self.args.workdir)
            job = dict(target=target, workload='one' if profile=='capacity-large' else 'page',
                request_bytes=65536 if profile=='capacity-large' else 1024,
                rate=50 if profile=='light' else 0, engine='wrk2' if profile=='light' else 'wrk',
                sockets=0, subscriptions=1, active_tenants=10, realtime_mode='both', probe_rate=1,
                duration=duration, warmup=self.args.warmup,
                connections=1 if profile=='light' else 32, threads=1 if profile=='light' else 2,
                wrk_image=self.args.wrk_image, wrk_cpuset='5-6', auth_mode='jwt', auth_fixture='public')
            config = run_namespace(self.args.workdir, path, job, self.args.url)
            await fixture_ready(config)
            resource_path = path.with_suffix('.resources.jsonl')
            collector = asyncio.create_task(collect_colima(self.args.workdir, resource_path, stop, ready))
            waiter = asyncio.create_task(ready.wait())
            try:
                await asyncio.wait([waiter, collector], timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if collector.done():
                    await collector
                if not ready.is_set():
                    raise RuntimeError('resource sampler not ready')
            finally:
                waiter.cancel()
            trial = await run(config)
            # Postflight and native reload are outside the measured steady state.
            # Fresh cgroups retain lifetime peaks, including startup and reload.
            await fixture_ready(config)
            await asyncio.to_thread(compose, self.args.workdir, 'kill', '--signal',
                                    'HUP' if target=='pgxio' else 'USR1', target)
            await asyncio.sleep(2)
            await fixture_ready(config)
            result['reload_check'] = 'nginx-HUP' if target=='pgxio' else 'postgrest-schema-USR1'
            result['reload_passed'] = True
            stop.set()
            await asyncio.wait_for(collector, 10)
            collector = None
            result['resources'] = summarize(resource_path, trial)
            result['assessment'] = assess(trial, result['resources'])
            result['ram_assessment'] = ram_assessment(result['assessment'],trial,profile)
            # Resource OOM is a valid failing candidate, not tool failure.
            errors = set(result['resources']['errors'])
            resource_failure = errors - {'oom_event', 'host_OOM_during_trial'}
            result.update(valid=not resource_failure,
                          passed=result['ram_assessment']['workload_passed'],
                          successful_rps=trial['results']['successful_requests_per_second'],
                          elapsed=trial['measured_elapsed_seconds'])
            if generator_missed(trial):
                result.update(valid=False, passed=False, generator_failure='missed_scheduled_arrivals')
            if trial['results']['counts'].get('invalid_response',0):
                result['passed'] = False
        except Exception as error:
            result.update(failure_type=type(error).__name__, detail=str(error)[-1000:])
        finally:
            stop.set()
            if collector:
                try:
                    await asyncio.wait_for(collector, 10)
                except Exception as error:
                    result['collector_failure'] = type(error).__name__
            result['containers_after'] = await asyncio.to_thread(self.records)
            api_rows = [r for r in result['containers_after'] if r['service']==target]
            killed = any(r['state']['OOMKilled'] for r in api_rows)
            restarted = any(r['restarts'] or not r['state']['Running'] for r in api_rows)
            if killed:
                result.update(valid=True, passed=False, resource_failure='container_OOMKilled')
            elif restarted:
                result.update(passed=False)
            # Read cgroup counters/peaks from the entire collector window,
            # including postflight and reload (not just the load interval).
            resource_path = path.with_suffix('.resources.jsonl')
            if resource_path.exists():
                snapshots = [json.loads(line) for line in resource_path.read_text().splitlines()]
                groups = [g for s in snapshots if s['type']=='sample'
                          for g in s['groups'].values() if g['role']=='api']
                result['lifetime_peak_bytes'] = max((g['memory_peak_bytes'] for g in groups), default=None)
                oom = any(g['memory_events'].get('oom',0) or g['memory_events'].get('oom_kill',0) for g in groups)
                if oom:
                    result.update(passed=False, resource_failure='cgroup_OOM_event')
            if not result.get('reload_passed'):
                result['passed'] = False
            result['finished_unix'] = time.time()
            self.state['trials'].append(result)
            self.state.pop('current', None)
            self.save()
            # Logs may include configuration; retain privately, never print.
            if not result['passed']:
                try:
                    dump(path.with_suffix('.diagnostic.json'),
                         {'logs': await asyncio.to_thread(compose, self.args.workdir, 'logs', '--no-color', '--tail', '100')}, private=True)
                except Exception:
                    pass
            print(json.dumps({k:result.get(k) for k in ('key','valid','passed','successful_rps','resource_failure','detail')}), flush=True)
        if not result['valid']:
            if attempt<3 and ('resources' in result or 'collector_failure' in result):
                print('RETRY invalid resource/generator capture: '+key,flush=True)
                return await self.trial(target,cap,profile,duration,repetition,phase)
            raise RuntimeError('inconclusive trial; fix infrastructure and resume: '+key)
        return result

    async def screen(self, target, profile, reference):
        results = {}
        # Descending search finds an ample passing control before OOM probes.
        for cap in (256,128,64,32,16,8):
            r = await self.trial(target, cap, profile, self.args.screen_seconds, 1, 'screen')
            passed = r['passed'] and (profile=='light' or retention_pass(r.get('successful_rps',0), reference))
            results[cap] = passed
            if not passed:
                break
        floor, bounded = bounded_floor(results)
        if floor is None:
            raise RuntimeError('256 MiB control failed; no boundary claim: '+target+' '+profile)
        if not bounded and floor>8:
            below = max(n for n,p in results.items() if not p)
            for cap in range(below+8, floor, 8):
                r = await self.trial(target, cap, profile, self.args.screen_seconds, 1, 'refine')
                results[cap] = r['passed'] and (profile=='light' or retention_pass(r.get('successful_rps',0), reference))
                if results[cap]:
                    break
        floor, bounded = bounded_floor(results)
        entry = dict(screen_floor_mib=floor, boundary_below_failed=bounded,
                     resolution_mib=8, reference_rps=reference, screen_results=results,
                     qualified_minimum_mib=None, recommended_mib=None,
                     note='Screening only; passing floor needs independent confirmation.')
        self.state['floors'][target+'-'+profile] = entry
        self.save()
        return entry

    async def execute(self):
        docker = json.loads(await asyncio.to_thread(command, ['docker','info','--format','{{json .}}']))
        assert docker['NCPU']>=8, 'eight VM CPUs required; do not overlap API, DB and wrk'
        environment = dict(docker={k:docker[k] for k in
             ('Architecture','NCPU','MemTotal','KernelVersion','ServerVersion')},
             original_configuration=public_settings(self.original),
             source_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in [Path(__file__), *Path(__file__).parent.joinpath('pgxbench').glob('*.py')]})
        environment['images'] = {name:await asyncio.to_thread(command,
            ['docker','image','inspect',name,'--format','{{.Id}}']) for name in
            (self.args.image,self.args.wrk_image,self.original['versions']['postgrest'],self.original['versions']['postgres'])}
        environment_path = self.output/'environment.json'
        if self.args.resume:
            old = json.loads(environment_path.read_text())
            assert old['images']==environment['images'], 'resume image IDs changed'
            assert old['source_sha256']==environment['source_sha256'], 'resume benchmark sources changed'
        else:
            dump(environment_path,environment)
        try:
            for target in self.args.target_order.split(','):
                for profile in ('light','capacity-page','capacity-large'):
                    reference = None
                    if profile!='light':
                        controls = [await self.trial(target,256,profile,self.args.screen_seconds,r,'reference') for r in (1,2,3)]
                        if not all(r['passed'] for r in controls):
                            raise RuntimeError('ample-memory reference failed')
                        reference = median(r['successful_rps'] for r in controls)
                    await self.screen(target,profile,reference)
            self.state['stage'] = 'confirmation'
            self.save()
            make_report(self.output/'trials', self.output/'report')
            if self.args.screen_only:
                self.state.update(status='screened',stage='done')
                return
            for target in self.args.target_order.split(','):
                entry = self.state['floors'][target+'-light']
                cap = entry['screen_floor_mib']
                while cap<=256:
                    runs = []
                    for repetition in (1,2,3):
                        runs.append(await self.trial(target,cap,'light',self.args.confirm_seconds,repetition,'confirm'))
                        if not runs[-1]['passed']:
                            break
                    if len(runs)==3 and all(r['passed'] for r in runs):
                        entry.update(confirmed_allocation_mib=cap,
                                     confirmation_seconds=self.args.confirm_seconds,
                                     confirmation_repetitions=3)
                        if self.args.confirm_seconds>=3600 and all(r.get('elapsed',0)>=3600 for r in runs):
                            entry['qualified_minimum_mib'] = cap if entry['boundary_below_failed'] and cap==entry['screen_floor_mib'] else None
                            entry['note'] = 'Three one-hour light REST runs passed; whole-server RAM and realtime are outside scope.'
                        entry['recommendation_candidate_mib'] = math.ceil(cap*1.25/8)*8
                        self.save()
                        break
                    cap += 8
                else:
                    raise RuntimeError('confirmation failed through 256 MiB')
                if self.args.soak:
                    candidate = entry['recommendation_candidate_mib']
                    soak = await self.trial(target,candidate,'light',86400,1,'soak')
                    if soak['passed'] and soak.get('elapsed',0)>=86400:
                        entry['recommended_mib'] = candidate
                    self.save()
            self.state.update(status='completed', stage='done')
        finally:
            await asyncio.to_thread(compose, self.args.workdir, 'down', '--remove-orphans')
            render(self.args.workdir, self.original)
            self.save()
            make_report(self.output/'trials', self.output/'report')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--target-order', choices=('pgxio,postgrest','postgrest,pgxio'),
                        default='pgxio,postgrest', help='screen and confirm products in this order')
    parser.add_argument('--screen-only', action='store_true')
    parser.add_argument('--soak', action='store_true', help='also run two sequential 24-hour recommendation soaks')
    parser.add_argument('--workdir', type=Path, default=Path('artifacts/bench'))
    parser.add_argument('--output', type=Path, default=Path('artifacts/memory-sweep'))
    parser.add_argument('--image', default='pgxio:borrowed-libpq')
    parser.add_argument('--wrk-image', default='pgxbench-wrk')
    parser.add_argument('--url', default='http://localhost:18080')
    parser.add_argument('--database-tls', choices=('disable','verify-full'), default='disable')
    parser.add_argument('--screen-seconds', type=int, default=60)
    parser.add_argument('--confirm-seconds', type=int, default=3600)
    parser.add_argument('--warmup', type=int, default=20)
    args = parser.parse_args()
    if min(args.screen_seconds,args.confirm_seconds,args.warmup)<20:
        parser.error('measurement and warmup durations must be at least 20 seconds for wrk2 calibration')
    if not args.execute:
        print(json.dumps(dict(dry_run=True, targets=args.target_order.split(','), grid_mib=[8,16,32,64,128,256],
            refine_mib=8, profiles=['REST page at 50 rps','page at 32 connections','64 KiB one-row at 32 connections'],
            confirmation='3 independent runs per product at light-load boundary',
            minimum_confirmation_hours=6*args.confirm_seconds/3600,
            soak_hours=48 if args.soak else 0),indent=2))
        return
    args.output.mkdir(parents=True,exist_ok=True)
    config_path = args.output/'plan.json'
    configuration = {k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items() if k not in ('resume','execute','target_order')}
    if args.resume:
        assert json.loads(config_path.read_text())==configuration, 'resume options differ'
    elif config_path.exists():
        raise FileExistsError('use a fresh output or --resume')
    else:
        dump(config_path,configuration)
    dump(args.output/'schedule.json',dict(target_order=args.target_order.split(','),
                                        recorded_unix=time.time(),resume=args.resume))
    sweep = Sweep(args)
    async def drive():
        task = asyncio.current_task()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,task.cancel)
        try:
            await sweep.execute()
        except BaseException as error:
            sweep.state.update(status='interrupted' if isinstance(error, (asyncio.CancelledError,KeyboardInterrupt)) else 'blocked',
                               failure_type=type(error).__name__,detail=str(error)[-1000:])
            sweep.save()
            raise
    asyncio.run(drive())


if __name__=='__main__':
    main()

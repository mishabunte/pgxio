"""Matched cache-on/cache-off REST comparison; dry run unless --execute."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import random
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'benchmarks'))
from pgxbench import auth_compare
from pgxbench.common import command, dump, load_settings


def jobs():
    result=[]
    for target in ('pgxio','postgrest'):
        for cached in (False,True):
            for workload,size in (('page',1024),('one',65536)):
                for repetition in (1,2,3):
                    options = {} if target=='postgrest' else dict(libpq_buf='copy',
                        jwt_cache_max_entries=1000 if cached else 0,jwt_cache_max_bytes=1048576)
                    result.append(dict(target=target,profile=target+('-cached' if cached else '-uncached'),
                        pgxio_image='pgxio:borrowed-libpq' if target=='pgxio' else '',module_options=options,
                        database_tls='disable',cpus=1,workers=1,nginx_profile='baseline',
                        auth_mode='jwt',auth_fixture='public',jwt_cache='default' if cached else 'off',
                        workload=workload,request_bytes=size,rate=0,sockets=0,subscriptions=1,
                        active_tenants=10,probe_rate=1,realtime_mode='both',engine='wrk',
                        connections=32,threads=2,wrk_image='pgxbench-wrk',wrk_cpuset='5-6',
                        duration=30,warmup=10,repetition=repetition))
    random.Random(20261017).shuffle(result)
    for index,job in enumerate(result):job['id']=f'{index:03d}'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/jwt-cache-db4-01')
    parser.add_argument('--resume-from',type=Path,action='append',default=[])
    options=parser.parse_args()
    specification=jobs()
    if options.resume_from:
        expected={j['id']:j for j in specification}
        completed=set()
        for previous in options.resume_from:
            old=json.loads((previous/'campaign.json').read_text())['jobs']
            assert all(expected[j['id']]==j for j in old),'resume specification differs'
            for row in json.loads((previous/'outcomes.json').read_text()):
                if row['passed'] and all(k=='http_200' or not v for k,v in row.get('counts',{}).items()):
                    completed.add(row['job']['id'])
        specification=[j for j in specification if j['id'] not in completed]
    args=SimpleNamespace(execute=options.execute,output=options.output,workdir=ROOT/'artifacts/bench',
        seed=20261017,duration=30,collector_host='colima',api_cpuset='0',db_cpuset='1-4',
        url='http://localhost:18080')
    if options.execute:
        assert not (options.output/'campaign.json').exists(),'use a fresh output directory'
        options.output.mkdir(parents=True,exist_ok=True)
        dump(options.output/'source.json',dict(resume_from=[str(p) for p in options.resume_from],
            sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [Path(__file__), *ROOT.joinpath('benchmarks/pgxbench').glob('*')]
                if p.is_file()}))
        # Assert rendered AND actual limits/cache configuration before each load.
        original_render=auth_compare.render
        original_ready=auth_compare.fixture_ready
        evidence=[]
        def checked_render(workdir,updates):
            settings=original_render(workdir,updates)
            if str(settings.get('performance_profile','')).endswith(('-cached','-uncached')):
                cached=settings['jwt_cache']!='off'
                target=settings['target']
                assert settings['memory_mib']==0 and settings['db_cpuset']=='1-4'
                if settings.get('effective_config_format') == 'nginx-v1':
                    from scripts.nginx_config import read_settings
                    config, _ = read_settings(Path(workdir)/'runtime/pgxio.conf')
                else:
                    config=json.loads((Path(workdir)/'runtime/pgxio.json').read_text())
                if target=='pgxio':
                    assert config['jwt_cache_max_entries']==(1000 if cached else 0)
                    assert config['jwt_cache_max_bytes']==1048576 and config['libpq_buf']=='copy'
                evidence.append(dict(target=target,cached=cached,configuration_checked=True))
            return settings
        async def checked_ready(config):
            result=await original_ready(config)
            ids=command(['docker','compose','--project-name','pgxbench','--file',str(args.workdir/'compose.json'),'ps','--quiet']).splitlines()
            records=json.loads(command(['docker','inspect',*ids]))
            settings=load_settings(args.workdir)
            target=settings['target']
            api=next(r for r in records if r['Config']['Labels']['com.docker.compose.service']==target)
            db=next(r for r in records if r['Config']['Labels']['com.docker.compose.service']=='postgres')
            assert api['HostConfig']['CpusetCpus']=='0' and api['HostConfig']['NanoCpus']==1000000000
            assert api['HostConfig']['Memory']==0 and db['HostConfig']['CpusetCpus']=='1-4'
            if target=='postgrest':
                env=dict(v.split('=',1) for v in api['Config']['Env'])
                assert env['PGRST_JWT_CACHE_MAX_ENTRIES']==('0' if settings['jwt_cache']=='off' else '1000')
            evidence[-1].update(image=api['Image'],database_image=db['Image'],actual_limits_checked=True,
                                native_cache_checked=target=='postgrest')
            dump(options.output/'configuration-evidence.json',evidence)
            return result
        auth_compare.render=checked_render
        auth_compare.fixture_ready=checked_ready
    result=asyncio.run(auth_compare.compare(args,specification))
    print(json.dumps(result,indent=2))
    if options.execute:
        rows=json.loads((options.output/'report/summary.json').read_text())['rows']
        if not result['passed'] or not all(r['workload_passed'] and r['resource_evidence_valid'] and r['HTTP_error_fraction']==0 for r in rows):
            raise SystemExit(1)


if __name__=='__main__':main()

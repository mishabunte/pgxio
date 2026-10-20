"""Paired HTTP gate for copy versus conservative auto buffer selection."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import random
from types import SimpleNamespace
import httpx
from pgxbench import auth_compare
from pgxbench.common import dump, load_settings
from pgxbench.load import Driver

ROOT=Path(__file__).resolve().parents[1]


def jobs(image,repetitions):
    rows=[]
    for tls in ('disable','verify-full'):
        for size in (1024,16384,65536):
            for mode in ('copy','auto'):
                for rep in range(1,repetitions+1):
                    rows.append(dict(id=f'{tls}-{size}-{mode}-{rep}',profile='buffer-'+mode,
                        target='pgxio',pgxio_image=image,module_options={'libpq_buf':mode},
                        database_tls=tls,workload='one',request_bytes=size,auth_mode='jwt',
                        jwt_cache='off',auth_fixture='public',rate=0,sockets=0,subscriptions=1,
                        active_tenants=10,probe_rate=1,realtime_mode='both',engine='wrk',
                        cpus=1,workers=1,connections=32,threads=2,wrk_image='pgxbench-wrk',
                        wrk_cpuset='5-6',duration=30,warmup=10,repetition=rep))
    random.Random(20261019).shuffle(rows)
    return rows


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--image',default='pgxio:buffer-modes')
    p.add_argument('--repetitions',type=int,choices=(5,7),default=5)
    p.add_argument('--resume-from',type=Path,action='append',default=[])
    p.add_argument('--output',type=Path,default=ROOT/'artifacts/buffer-compare-01')
    options=p.parse_args()
    specification=jobs(options.image,options.repetitions)
    expected={j['id']:j for j in specification}
    retained=set()
    for source in options.resume_from:
        for j in json.loads((source/'campaign.json').read_text())['jobs']:
            assert expected[j['id']]==j,'resume specification differs'
        for r in json.loads((source/'outcomes.json').read_text()):
            if r['passed'] and not any(k!='http_200' and v for k,v in r['counts'].items()):retained.add(r['job']['id'])
    specification=[j for j in specification if j['id'] not in retained]
    args=SimpleNamespace(execute=options.execute,output=options.output,workdir=ROOT/'artifacts/bench',
        seed=20261019,duration=30,collector_host='colima',api_cpuset='0',db_cpuset='1-4',url='http://localhost:18080')
    if options.execute:
        if (options.output/'campaign.json').exists():raise FileExistsError('use a fresh output directory')
        options.output.mkdir(parents=True,exist_ok=True)
        sources=[Path(__file__),*ROOT.joinpath('crates/pgxio-module/src').glob('*.rs'),
                 ROOT/'crates/pgxio-module/native/adapter.c',*ROOT.joinpath('benchmarks/pgxbench').glob('*')]
        dump(options.output/'source.json',dict(sha256={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sources if f.is_file()},
             resume_from=[str(s) for s in options.resume_from]))
        original=auth_compare.fixture_ready
        checks=[]
        async def checked_ready(config):
            result=await original(config)
            settings=load_settings(args.workdir)
            mode=settings['module_options']['libpq_buf']
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    before=(await client.get(args.url+'/__pgxio_buffers')).json()
                    if before['plaintext'] or settings['database_tls']!='disable':break
                    await asyncio.sleep(.05)
                assert before['plaintext']==(settings['database_tls']=='disable'),'actual pool transport differs'
                driver=Driver(config)
                try:
                    await driver.call('bench_one',{'id':1},measure=False)
                finally:
                    await driver.close()
                after=(await client.get(args.url+'/__pgxio_buffers')).json()
                selected='borrowed' if after['borrowed']>before['borrowed'] else 'copy'
                must_copy=mode=='copy' or settings['database_tls']!='disable' or config.request_bytes<16384
                assert selected==('copy' if must_copy else 'borrowed'),'unexpected actual body selection'
                assert after['copy']+after['borrowed']-before['copy']-before['borrowed']==1
                checks.append(dict(mode=mode,database_tls=settings['database_tls'],body_bytes=config.request_bytes,selected=selected))
                dump(options.output/'selection-evidence.json',checks)
            return result
        auth_compare.fixture_ready=checked_ready
    result=asyncio.run(auth_compare.compare(args,specification))
    print(json.dumps(result,indent=2))
    if options.execute:
        rows=json.loads((options.output/'report/summary.json').read_text())['rows']
        if not result['passed'] or not all(r['HTTP_error_fraction']==0 and r['resource_evidence_valid'] and r['workload_passed'] for r in rows):raise SystemExit(1)


if __name__=='__main__':main()

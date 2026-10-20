"""Run the borrowed/copy matrix without sockets, a database or HTTP."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]


def cases(repetitions=5,smoke=False,include_auto=False):
    rows=[]
    for stage in ('input','libpq','combined'):
        for transport in (('plain',) if stage=='input' else ('plain','tls')):
            for size in ((1024,16384,65536) if smoke and include_auto else (1024,) if smoke else (256,1024,4096,16384,65536)):
                for segments in ((1,16) if smoke else (1,4,16)):
                    for mode in (('copy','borrowed','auto') if include_auto else ('copy','borrowed')):
                        for rep in range(1,repetitions+1):
                            rows.append(dict(stage=stage,transport=transport,body_bytes=size,
                                             segments=segments,mode=mode,repetition=rep))
    random.Random(20261018).shuffle(rows)
    return rows


def run_batch(args,rows,diagnostics):
    output=args.output.resolve()
    name='diagnostics' if diagnostics else 'timing'
    duration=.02 if args.smoke else .5
    warmup=.005 if args.smoke else .1
    path=output/(name+'-cases.txt')
    path.write_text(''.join(f"{j['stage']} {j['mode']} {j['transport']} {j['body_bytes']} {j['segments']} {warmup} {duration}\n" for j in rows))
    binary='borrowed-synthetic-diagnostics' if diagnostics else 'borrowed-synthetic'
    script=f'while read -r stage mode transport size segments warmup duration; do {binary} "$stage" "$mode" "$transport" "$size" "$segments" "$warmup" "$duration"; done < /results/{path.name}'
    argv=['docker','run','--rm','--network','none','--cpuset-cpus',args.cpuset,
          '-v',f'{output}:/results','--entrypoint','sh']
    if diagnostics:argv+=['-e','LD_PRELOAD=/usr/local/lib/synthetic-allocations.so']
    argv += [args.image,'-ec',script]
    results=[]
    with (output/(name+'.stderr')).open('w') as error, (output/(name+'.jsonl')).open('w') as stream:
        process=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=error,text=True)
        try:
            for line in process.stdout:
                row=json.loads(line)
                assert len(results)<len(rows),'unexpected extra measurement'
                job=rows[len(results)]
                assert all(row[k]==job[k] for k in ('stage','mode','body_bytes','segments'))
                assert row['transport']==('none' if job['stage']=='input' else job['transport'])
                assert row['diagnostics']==diagnostics and row['validated'] and row['socket_operations']==0
                assert row['operations']>0
                if job['mode']=='auto':
                    borrow=(job['stage']=='input' or job['transport']=='plain') and job['body_bytes']>=16384 and job['segments']<=job['body_bytes']//4096
                    assert row['effective_mode']==('borrowed' if borrow else 'copy'),'auto selected an unexpected path'
                if diagnostics and row['stage']=='input':
                    assert row['allocation_calls_per_op']==1,'unexpected input allocation count'
                    expected=row['body_bytes']+1 if row.get('effective_mode',row['mode'])=='copy' else 16*row['segments']
                    assert row['allocated_bytes_per_op']==expected,'unexpected input allocation volume'
                row['repetition']=job['repetition']
                results.append(row);stream.write(json.dumps(row)+'\n');stream.flush()
                if len(results)%25==0 or len(results)==len(rows):
                    print(f'{name}: {len(results)}/{len(rows)}',flush=True)
            assert process.wait()==0,f'{name} failed; see {name}.stderr'
        finally:
            if process.poll() is None:
                process.terminate();process.wait(timeout=10)
        assert len(results)==len(rows),'incomplete matrix'
    return results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--smoke',action='store_true')
    p.add_argument('--auto',action='store_true',help='also exercise the production auto selector')
    p.add_argument('--image',default='pgxio-synthetic:borrowed')
    p.add_argument('--output',type=Path,default=ROOT/'artifacts/borrowed-synthetic')
    p.add_argument('--cpuset',default='0')
    args=p.parse_args()
    timing=cases(1 if args.smoke else 5,args.smoke,args.auto)
    diagnostics=cases(1,args.smoke,args.auto)
    if not args.execute:
        print(json.dumps(dict(dry_run=True,timing_trials=len(timing),diagnostic_cases=len(diagnostics),
              network='none',stages=['input','libpq','combined'],tls='real TLS 1.3 encryption to an in-memory sink'),indent=2));return
    if (args.output/'environment.json').exists():raise FileExistsError('use a fresh output directory')
    args.output.mkdir(parents=True,exist_ok=True)
    image=json.loads(subprocess.check_output(['docker','image','inspect',args.image],text=True))[0]
    build_meta=subprocess.check_output(['docker','run','--rm','--network','none','--entrypoint','cat',
                                       args.image,'/synthetic/build-meta.txt'],text=True)
    compiled_files={'/synthetic/fixture.c':ROOT/'benchmarks/synthetic/fixture.c',
        '/crates/pgxio-module/src/input.rs':ROOT/'crates/pgxio-module/src/input.rs',
        '/crates/pgxio-module/src/borrowed.rs':ROOT/'crates/pgxio-module/src/borrowed.rs',
        '/crates/pgxio-module/src/buffer.rs':ROOT/'crates/pgxio-module/src/buffer.rs',
        '/synthetic/src/main.rs':ROOT/'benchmarks/synthetic/src/main.rs',
        '/synthetic/Cargo.lock':ROOT/'benchmarks/synthetic/Cargo.lock'}
    for line in build_meta.splitlines():
        if '  /' in line:
            digest,name=line.split('  ',1)
            if name in compiled_files:
                assert hashlib.sha256(compiled_files.pop(name).read_bytes()).hexdigest()==digest,'image source differs: '+name
    assert not compiled_files,'missing image source identities'
    files=[Path(__file__),ROOT/'crates/pgxio-module/src/input.rs',ROOT/'crates/pgxio-module/src/borrowed.rs',ROOT/'crates/pgxio-module/src/buffer.rs',
           *ROOT.joinpath('benchmarks/synthetic').rglob('*')]
    environment=dict(started_unix=time.time(),image_id=image['Id'],image=args.image,cpuset=args.cpuset,build_meta=build_meta,
        architecture=image['Architecture'],network='none',smoke=args.smoke,
        sources={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.is_file()},
        limitations=['Synthetic local CPU costs, not HTTP throughput','No PostgreSQL execution or response parsing',
                     'TLS handshake and peer decryption excluded from timing','Allocation counters run separately; requested bytes are not RSS',
                     'No batching/backpressure; prepared statement send and full flush per operation'])
    (args.output/'environment.json').write_text(json.dumps(environment,indent=2)+'\n')
    (args.output/'plan.json').write_text(json.dumps(dict(timing=timing,diagnostics=diagnostics),indent=2)+'\n')
    run_batch(args,timing,False)
    run_batch(args,diagnostics,True)
    environment['finished_unix']=time.time()
    (args.output/'environment.json').write_text(json.dumps(environment,indent=2)+'\n')
    subprocess.run(['python3',str(ROOT/'benchmarks/summarize-synthetic.py'),str(args.output)],check=True)


if __name__=='__main__':main()

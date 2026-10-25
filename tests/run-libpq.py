#!/usr/bin/env python3
"""Exercise borrowed libpq in an isolated PostgreSQL 17 project, then clean up."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import random
import hashlib

ROOT = Path(__file__).resolve().parents[1]

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image', default='pgxio-libpq:borrowed')
    p.add_argument('--sanitized', action='store_true')
    p.add_argument('--faults', action='store_true', help='inject short writes, EINTR and EAGAIN (Linux test shim)')
    p.add_argument('--benchmark', action='store_true', help='matched client CPU microbenchmark, separate from correctness tests')
    p.add_argument('--control-image', default='pgxio-libpq:control')
    p.add_argument('--duration', type=float, default=3)
    p.add_argument('--warmup', type=float, default=1)
    p.add_argument('--repetitions', type=int, default=3)
    p.add_argument('--output', type=Path, default=ROOT/'artifacts/libpq-validation')
    args = p.parse_args()
    if args.duration <= 0 or args.warmup < 0 or args.repetitions < 1:
        p.error('invalid duration, warmup or repetitions')
    if args.benchmark and (args.sanitized or args.faults):
        p.error('benchmark timing must not use sanitizers or fault injection')
    if args.sanitized and args.faults:
        p.error('run allocation-fault injection separately from the sanitizer allocator')
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    if args.benchmark and (output/'measurements.json').exists():
        p.error('use a fresh output directory for each benchmark')
    password = secrets.token_hex(24)
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2',
        '-keyout',str(output/'key.pem'),'-out',str(output/'cert.pem'),'-subj','/CN=postgres',
        '-addext','subjectAltName=DNS:postgres'],check=True,capture_output=True)
    (output/'key.pem').chmod(0o600)
    (output/'entrypoint.sh').write_text('''#!/bin/sh
set -eu
mkdir -p /tmp/tls
cp /fixtures/key.pem /tmp/tls/key.pem
cp /fixtures/cert.pem /tmp/tls/cert.pem
chmod 600 /tmp/tls/key.pem
chown -R postgres:postgres /tmp/tls
exec /usr/local/bin/docker-entrypoint.sh "$@"
''')
    flags = '-fsanitize=address,undefined -fno-omit-frame-pointer' if args.sanitized else ''
    client = ('cc -O1 -g '+flags+' -I/postgres/src/include -I/opt/pgxio-libpq/include '
        '-I/postgres/src/interfaces/libpq /tests/test_borrowed.c '
        '-L/opt/pgxio-libpq/lib -lpq -o /tmp/test-borrowed && '
        '/tmp/test-borrowed "$PGXIO_LIBPQ_TEST_URL"')
    if args.faults:
        client = ('cc -shared -fPIC /tests/test_faults.c -ldl -o /tmp/faults.so && ' +
                  client.replace('/tmp/test-borrowed "$PGXIO_LIBPQ_TEST_URL"',
                  'LD_PRELOAD=/tmp/faults.so LIBPQ_TEST_SHORT_WRITES=1 /tmp/test-borrowed "$PGXIO_LIBPQ_TEST_URL"'))
    document = {'services': {
        'postgres': {'image':'postgres:17.11-bookworm',
            'environment':{'POSTGRES_PASSWORD':password},
            'volumes':[f'{output}:/fixtures:ro'],
            'entrypoint':['sh','/fixtures/entrypoint.sh'],
            'command':['postgres','-c','ssl=on','-c','ssl_cert_file=/tmp/tls/cert.pem','-c','ssl_key_file=/tmp/tls/key.pem'],
            'healthcheck':{'test':['CMD-SHELL','pg_isready -U postgres'],'interval':'1s','timeout':'2s','retries':30}},
        'client': {'image':args.image,'entrypoint':['sh','-ec'], 'command':[client],
            'environment':{'LD_LIBRARY_PATH':'/opt/pgxio-libpq/lib',
                           'ASAN_OPTIONS':'detect_leaks=1:abort_on_error=1',
                           'UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'},
            'volumes':[f'{ROOT}/patches/libpq:/tests:ro',f'{output}/cert.pem:/ca.pem:ro']}}}
    if args.benchmark:
        document['services']['postgres']['cpuset']='1'
    path = output/'compose.json'; path.touch(mode=0o600); path.chmod(0o600)
    path.write_text(json.dumps(document))
    base = ['docker','compose','--project-name','pgxio-libpq-test','--file',str(path)]
    results = []
    try:
        subprocess.run(base+['up','--detach','--wait','postgres'],check=True,capture_output=True)
        if args.benchmark:
            cases = [(profile,image,mode,size,segments,repetition)
                for profile,image in [('control-copy',args.control_image),('patched-copy',args.image),('patched-borrowed',args.image)]
                for mode in ('disable','verify-full') for size in (1024,16384,65536,1048576)
                for segments in (1,16) for repetition in range(1,args.repetitions+1)]
            random.Random(20261016).shuffle(cases)
            identities={name:json.loads(subprocess.check_output(['docker','image','inspect',name]))[0]['Id']
                for name in {args.image,args.control_image}}
            (output/'environment.json').write_text(json.dumps({'images':identities,
                'source_sha256':hashlib.sha256((ROOT/'patches/libpq/bench.c').read_bytes()).hexdigest(),
                'note':'Dedicated PostgreSQL 17 text-length SQL; client process CPU only. Not pgxio throughput or API saturation.'},indent=2))
            for index,(profile,image,mode,size,segments,repetition) in enumerate(cases):
                print(f'BENCH {index+1}/{len(cases)} {profile} {mode} {size}B {segments} segments',flush=True)
                compile_and_run=('cc -O2 -I/opt/pgxio-libpq/include /tests/bench.c -L/opt/pgxio-libpq/lib -lpq -o /tmp/bench && '
                    f'/tmp/bench "$PGXIO_LIBPQ_TEST_URL" {size} {segments} {args.duration} {args.warmup} '+
                    ('borrowed' if profile=='patched-borrowed' else 'copy'))
                url=f'postgresql://postgres:{password}@postgres/postgres?sslmode={mode}&sslrootcert=/ca.pem'
                run=subprocess.run(['docker','run','--rm','--cpuset-cpus','0','--network','pgxio-libpq-test_default',
                    '-v',f'{ROOT}/patches/libpq:/tests:ro','-v',f'{output}/cert.pem:/ca.pem:ro',
                    '-e','PGXIO_LIBPQ_TEST_URL','-e','LD_LIBRARY_PATH=/opt/pgxio-libpq/lib',
                    '--entrypoint','sh',image,'-ec',compile_and_run],
                    env=dict(os.environ,PGXIO_LIBPQ_TEST_URL=url),capture_output=True,text=True,timeout=args.duration+args.warmup+60)
                (output/f'{index:03d}.log').write_text(run.stdout+run.stderr)
                if run.returncode: raise RuntimeError(f'microbenchmark {index} failed')
                value=json.loads(run.stdout.strip())
                assert value['ssl']==(mode=='verify-full') and value['requests']>0
                value.update(profile=profile,database_tls=mode,repetition=repetition,passed=True,image_id=identities[image])
                results.append(value)
                (output/'measurements.json').write_text(json.dumps(results,indent=2)+'\n')
        for mode in (() if args.benchmark else ('disable','verify-full')):
            url = f'postgresql://postgres:{password}@postgres/postgres?sslmode={mode}&sslrootcert=/ca.pem'
            result = subprocess.run(base+['run','--rm','--no-deps','-e','PGXIO_LIBPQ_TEST_URL', 'client'],
                env=dict(os.environ,PGXIO_LIBPQ_TEST_URL=url),capture_output=True,text=True,timeout=180)
            (output/f'{mode}.log').write_text(result.stdout+result.stderr)
            print(f'{mode}: exit={result.returncode}',flush=True)
            results.append({'database_tls':mode,'passed':result.returncode==0})
            if result.returncode:
                raise RuntimeError(f'{mode} failed; see {output}/{mode}.log')
    finally:
        subprocess.run(base+['down','--remove-orphans'],check=True,capture_output=True)
        (output/'outcomes.json').write_text(json.dumps(results,indent=2)+'\n')
    if args.benchmark:
        import sys
        sys.path.insert(0,str(ROOT/'benchmarks'))
        from pgxbench.libpq_report import report
        report(results,output)

if __name__ == '__main__': main()

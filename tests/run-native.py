#!/usr/bin/env python3
"""Run existing regressions against a private, native PostgreSQL/Nginx fixture."""
import argparse
import base64
import json
import os
from pathlib import Path
import platform
import pwd
import secrets
import shlex
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.nginx_config import read_settings, render


def run(argv, **kw):
    return subprocess.run(list(map(str, argv)), check=True, capture_output=True, **kw)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prefix', type=Path, default=Path('/opt/pgxio'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--production', action='store_true')
    p.add_argument('--libpq-buf', choices=('copy', 'borrowed', 'auto'))
    p.add_argument('--schema', action='store_true')
    p.add_argument('--faults', action='store_true')
    p.add_argument('--c-contract-prefix', type=Path)
    p.add_argument('--suite', choices=('all', 'configuration', 'integration', 'performance', 'input', 'output', 'buffers', 'smoke'), default='all')
    p.add_argument('--profile', choices=('default','sequential','unprepared','cache','failfast'), default='default')
    args = p.parse_args()
    prefix=args.prefix.resolve(); directory=args.output.resolve(); runtime=directory/'runtime'
    directory.mkdir(parents=True, exist_ok=True); runtime.mkdir(exist_ok=True)
    if os.geteuid() == 0:
        try: account=pwd.getpwnam('pgxio')
        except KeyError:
            run(['pw','useradd','pgxio','-m'] if platform.system()=='FreeBSD' else ['useradd','--create-home','pgxio'])
            account=pwd.getpwnam('pgxio')
        for path in (directory, runtime): os.chown(path, account.pw_uid, account.pw_gid)
        command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
        child_env=dict(os.environ,HOME=account.pw_dir,USER=account.pw_name,LOGNAME=account.pw_name)
        return subprocess.call(['su','-m','pgxio','-c',shlex.join(command)],env=child_env)
    build=json.loads((prefix/'build.json').read_text()); patched=build['libpq']=='borrowed'
    if args.faults and not patched: p.error('faults require patched libpq')
    pg_bin = Path('/usr/lib/postgresql/17/bin') if platform.system()=='Linux' else Path('/usr/local/bin')
    postgres=directory/'postgres'
    if postgres.exists(): raise FileExistsError('use a fresh native output directory')
    run([pg_bin/'initdb','-D',postgres,'-U','postgres','--auth=trust','--encoding=UTF8','--no-locale'])
    secret,password=secrets.token_hex(32),secrets.token_hex(32)
    config,routes=read_settings(ROOT/'examples/pgxio.conf')
    config.update(pool_size=4, validate_rest_input=args.schema, validate_output=args.schema, development=not args.production)
    if args.profile=='sequential': config['db_pipeline']=False
    if args.profile=='unprepared': config['db_prepared_statements']=False
    if args.profile=='cache': config['jwt_cache_max_entries']=1000
    if args.profile=='failfast': config['pool_acquisition_timeout_ms']=0
    if args.libpq_buf: config['libpq_buf']=args.libpq_buf
    mode=args.libpq_buf or 'auto'
    contract=json.loads((ROOT/'examples/contract.json').read_text())
    for name in ('perf_probe','input_probe','borrowed_missing','output_probe','output_wrong_type','output_many_rows','output_no_rows'):
        contract['endpoints'].append(dict(name=name,input={'type':'object'},output={}))
        routes['endpoints'].append(dict(name=name,function='web.'+name))
    # The perf invalid-output fixture must fail enabled validation.
    contract['endpoints'][-7]['output']={'type':'object','required':['pid','subject','prepared','readonly']}
    (runtime/'contract.json').write_text(json.dumps(contract))
    config['contract']=str(runtime/'contract.json')
    nginx=(ROOT/'deploy/nginx.conf').read_text().replace('/usr/local/nginx/modules/', str(prefix/'modules')+'/')
    nginx=nginx.replace('/etc/pgxio/',str(runtime)+'/').replace('/tmp/nginx.pid',str(runtime/'nginx.pid'))
    nginx=nginx.replace('error_log stderr warn;',f'error_log {runtime}/nginx.log notice;')
    nginx=nginx.replace('/tmp/fastcgi',str(runtime/'fastcgi')).replace('/tmp/uwsgi',str(runtime/'uwsgi')).replace('/tmp/scgi',str(runtime/'scgi'))
    database_url=f'postgresql://web:{password}@localhost/pgxio?hostaddr=127.0.0.1&sslmode=disable'
    pg_options=['-h','127.0.0.1','-p','5432','-k',''] # TCP-only: host bind mounts need not support Unix sockets.
    env=dict(os.environ, PGXIO_TEST_BACKEND='native', PGXIO_TEST_NATIVE=str(directory/'native.json'),
        PGXIO_TEST_RUNTIME=str(runtime),PGXIO_TEST_CONFIG_DIR=str(runtime), PGXIO_TEST_COMPOSE_FILE=str(directory/'compose.json'),
        PGXIO_TEST_URL='http://127.0.0.1:8080',PGXIO_TEST_DEV_SECRET=secret,PGXIO_TEST_PROFILE=args.profile,
        PGXIO_TEST_BUFFER_MODE=mode,PGXIO_TEST_PATCHED=str(int(patched)),
        PGXIO_TEST_DIAGNOSTICS=str(int('benchmark-auth' in build.get('features',''))),
        PGXIO_TEST_BORROWED=str(int(patched and mode!='copy' and not args.schema and (mode=='borrowed' or not args.production))),
        PGXIO_TEST_INPUT_VALIDATION='schema' if args.schema else 'postgres',PGXIO_TEST_OUTPUT_VALIDATION='schema' if args.schema else 'off',
        PGXIO_TEST_HTTP2=str(int(args.production)),PGXIO_TEST_FAULTS=str(int(args.faults)),
        PYTHONPATH=str(ROOT/'sdk/python'))
    if args.production:
        run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-keyout',runtime/'tls-key.pem','-out',runtime/'tls-cert.pem','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'])
        (runtime/'tls-key.pem').chmod(0o600)
        run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',directory/'jwt-private.pem'])
        (directory/'jwt-private.pem').chmod(0o600)
        modulus=run(['openssl','rsa','-in',directory/'jwt-private.pem','-noout','-modulus'],text=True).stdout.strip().split('=',1)[1]
        enc=lambda v:base64.urlsafe_b64encode(v).rstrip(b'=').decode()
        (runtime/'jwks.json').write_text(json.dumps({'keys':[dict(kty='RSA',kid='local-test',alg='RS256',use='sig',n=enc(bytes.fromhex(modulus)),e='AQAB')]}))
        config['jwks_file']=str(runtime/'jwks.json')
        nginx=nginx.replace('listen 8080;',f'listen 8080; listen 8443 ssl; http2 on; ssl_certificate {runtime}/tls-cert.pem; ssl_certificate_key {runtime}/tls-key.pem; ssl_protocols TLSv1.3;')
        pg_options+=['-c','ssl=on','-c',f'ssl_cert_file={runtime}/tls-cert.pem','-c',f'ssl_key_file={runtime}/tls-key.pem']
        database_url=database_url.replace('sslmode=disable',f'sslmode=verify-full&sslrootcert={runtime}/tls-cert.pem')
        env.update(PGXIO_TEST_URL='https://localhost:8443',PGXIO_TEST_RSA_KEY=str(directory/'jwt-private.pem'),SSL_CERT_FILE=str(runtime/'tls-cert.pem'))
    api_env=dict(PGXIO_DATABASE_URL=database_url,PGXIO_DEV_SECRET=secret)
    if args.faults:
        shim=directory/'faults.so'
        compiler=['cc','-shared','-fPIC','-DPGXIO_IO_ONLY',str(ROOT/'patches/libpq/test_faults.c'),'-o',str(shim)]
        if platform.system()=='Linux': compiler+=['-ldl']
        run(compiler)
        api_env.update(LD_PRELOAD=str(shim),LIBPQ_TEST_BLOCK_FILE=str(runtime/'block'),LIBPQ_TEST_ALLOW_PREFIX='1')
        nginx='env LIBPQ_TEST_BLOCK_FILE;\nenv LIBPQ_TEST_ALLOW_PREFIX;\n'+nginx
    (runtime/'nginx.conf').write_text(nginx)
    (runtime/'pgxio.conf').write_text(render(config,routes))
    manifest=dict(runtime=str(runtime),nginx=str(prefix/'bin/nginx'),pg_bin=str(pg_bin),api_env=api_env)
    (directory/'native.json').write_text(json.dumps(manifest)); (directory/'native.json').chmod(0o600)
    operator=[sys.executable,str(ROOT/'tests/native-operator.py')]
    results=[]; started=False; complete=False
    try:
        run([pg_bin/'pg_ctl','-D',postgres,'-l',directory/'postgres.log','-o',shlex.join(pg_options),'-w','start']); started=True
        def sql(text, db='postgres', extra=()):
            return run([pg_bin/'psql','-h','127.0.0.1','-U','postgres','-d',db,'-v','ON_ERROR_STOP=1',*extra],input=text,text=True)
        sql('CREATE DATABASE pgxio;')
        sql((ROOT/'sql/example.sql').read_text(),'pgxio',('-v','web_password='+password))
        sql((ROOT/'tests/performance.sql').read_text(),'pgxio')
        run(operator+['up','api'],env=env)
        import httpx
        with httpx.Client(verify=env.get('SSL_CERT_FILE',True)) as client:
            for _ in range(200):
                try:
                    if client.get(env['PGXIO_TEST_URL']+'/readyz').status_code==200: break
                except httpx.HTTPError: pass
                time.sleep(.05)
            else: raise RuntimeError('native API did not become ready')
        if args.c_contract_prefix:
            diagnostic=args.c_contract_prefix.resolve()
            source=json.loads((ROOT/'patches/libpq/source.json').read_text())
            src=diagnostic/'build'/('postgres-'+source['ref'])
            exe=directory/'contract-client'
            run(['cc','-O1','-g','-I'+str(src/'src/include'),'-I'+str(diagnostic/'libpq/include'),'-I'+str(src/'src/interfaces/libpq'),ROOT/'patches/libpq/test_borrowed.c','-L'+str(diagnostic/'libpq/lib'),'-Wl,-rpath,'+str(diagnostic/'libpq/lib'),'-lpq','-o',exe])
            ce=dict(os.environ,LD_LIBRARY_PATH=str(diagnostic/'libpq/lib'),LIBPQ_TEST_SHORT_WRITES='1',LD_PRELOAD=api_env['LD_PRELOAD'])
            conn=database_url.replace('web:'+password+'@','postgres@')
            r=subprocess.run([str(exe),conn],env=ce,capture_output=True,text=True,timeout=300)
            (directory/'c-contract.log').write_text(r.stdout+r.stderr)
            results.append(dict(suite='c-ownership-allocation-write-faults',passed=r.returncode==0))
            if r.returncode: raise RuntimeError('C contract failed: '+r.stderr[-3000:])
        suites=['configuration','integration','performance','input','output','buffers'] if args.suite=='all' else [args.suite]
        for suite in suites:
            if suite in ('performance','input') and config['pool_size']!=1:
                config['pool_size']=1; (runtime/'pgxio.conf').write_text(render(config,routes)); run(operator+['up','api'],env=env)
            result=subprocess.run([sys.executable,str(ROOT/'tests'/(suite+'.py'))],env=env,cwd=ROOT,capture_output=True,text=True)
            (directory/(suite+'.log')).write_text(result.stdout+result.stderr)
            results.append(dict(suite=suite,passed=result.returncode==0))
            print(suite+': '+('PASS' if not result.returncode else 'FAIL'),flush=True)
            if result.returncode: print((result.stdout+result.stderr)[-9000:],flush=True); break
        logs=(runtime/'nginx.log').read_text()
        event='kqueue' if platform.system()=='FreeBSD' else 'epoll'
        assert f'using the "{event}" event method' in logs, 'actual Nginx event backend was not confirmed'
        results.append(dict(suite='event-backend',passed=True,backend=event))
        complete=True
    finally:
        if (runtime/'nginx.pid').exists(): subprocess.run(operator+['stop','api'],env=env,capture_output=True)
        if started: subprocess.run([pg_bin/'pg_ctl','-D',str(postgres),'-m','immediate','-w','stop'],capture_output=True)
        (directory/'outcomes.json').write_text(json.dumps(dict(build=build,mode=mode,production=args.production,schema=args.schema,faults=args.faults,suites=results,passed=complete and bool(results) and all(r['passed'] for r in results)),indent=2))
    return 0 if results and all(r['passed'] for r in results) else 1


if __name__=='__main__': sys.exit(main())

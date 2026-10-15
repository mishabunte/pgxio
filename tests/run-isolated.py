#!/usr/bin/env python3
"""Run HTTP/WSS and database regressions without touching the development stack."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='pgxio:perf-auth')
    parser.add_argument('--profile', choices=('default', 'sequential', 'unprepared', 'cache', 'failfast'), default='default')
    parser.add_argument('--production', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/perf-validation')
    parser.add_argument('--suite', choices=('integration', 'performance', 'all'), default='all')
    args = parser.parse_args()
    directory = args.output.resolve(); runtime = directory / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    secret, password = secrets.token_hex(32), secrets.token_hex(32)
    config = json.loads((ROOT / 'examples/pgxio.json').read_text())
    config.update(pool_size=1 if args.suite == 'performance' else 4, pool_queue_limit=32,
                  development=not args.production)
    if args.profile == 'sequential': config['db_pipeline'] = False
    if args.profile == 'unprepared': config['db_prepared_statements'] = False
    if args.profile == 'cache': config['jwt_cache_max_entries'] = 1000
    if args.profile == 'failfast': config['pool_acquisition_timeout_ms'] = 0
    contract=json.loads((ROOT / 'examples/contract.json').read_text())
    contract['endpoints'].append({'name':'perf_probe','function':'web.perf_probe','input':{'type':'object'},
        'output':{'type':'object','required':['pid','subject','prepared','readonly'], 'properties':{
            'pid':{'type':'integer'},'subject':{'type':'string'},'prepared':{'type':'integer'},'readonly':{'type':'string'}}}})
    (runtime / 'contract.json').write_text(json.dumps(contract))
    command = ['docker', 'compose', '--project-name', 'pgxio-perf-test', '--file', str(directory / 'compose.json')]
    def run(argv, **kwargs):
        return subprocess.run(argv, check=True, capture_output=True, **kwargs)
    env = dict(os.environ, PGXIO_TEST_COMPOSE_FILE=str(directory / 'compose.json'),
               PGXIO_TEST_PROJECT='pgxio-perf-test', PGXIO_TEST_DEV_SECRET=secret,
               PGXIO_TEST_URL='http://127.0.0.1:18090',
               PGXIO_TEST_PROFILE=args.profile,
               PYTHONPATH=str(ROOT / 'sdk/python') + os.pathsep + os.environ.get('PYTHONPATH', ''))
    nginx = (ROOT / 'deploy/nginx.conf').read_text()
    database_url = f'postgresql://web:{password}@postgres/pgxio?hostaddr=172.29.89.2&sslmode=disable'
    postgres_command = ['postgres']
    if args.production:
        run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
             '-keyout',str(runtime/'tls-key.pem'),'-out',str(runtime/'tls-cert.pem'),'-subj','/CN=localhost',
             '-addext','subjectAltName=DNS:localhost,DNS:postgres,IP:127.0.0.1,IP:172.29.89.2'])
        (runtime/'tls-key.pem').chmod(0o644)
        private=directory/'jwt-private.pem'
        run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(private)])
        private.chmod(0o600)
        modulus=run(['openssl','rsa','-in',str(private),'-noout','-modulus'],text=True).stdout.strip().split('=',1)[1]
        enc=lambda value:base64.urlsafe_b64encode(value).rstrip(b'=').decode()
        (runtime/'jwks.json').write_text(json.dumps({'keys':[{'kty':'RSA','kid':'local-test','alg':'RS256','use':'sig','n':enc(bytes.fromhex(modulus)),'e':'AQAB'}]}))
        config['jwks_file']='/etc/pgxio/jwks.json'
        nginx=nginx.replace('listen 8080;', 'listen 8080;\n        listen 8443 ssl;\n        ssl_certificate /etc/pgxio/tls-cert.pem;\n        ssl_certificate_key /etc/pgxio/tls-key.pem;\n        ssl_protocols TLSv1.3;')
        database_url=database_url.replace('sslmode=disable','sslmode=verify-full&sslrootcert=/etc/pgxio/tls-cert.pem')
        postgres_command=['postgres','-c','ssl=on','-c','ssl_cert_file=/tmp/db-tls/cert.pem','-c','ssl_key_file=/tmp/db-tls/key.pem']
        env.update(PGXIO_TEST_URL='https://localhost:18490', PGXIO_TEST_RSA_KEY=str(private), SSL_CERT_FILE=str(runtime/'tls-cert.pem'))
    (runtime/'pgxio.json').write_text(json.dumps(config))
    (runtime/'nginx.conf').write_text(nginx)
    (runtime/'db-entrypoint.sh').write_text('''#!/bin/sh
set -eu
if [ -f /fixtures/tls-key.pem ]; then
 mkdir -p /tmp/db-tls
 cp /fixtures/tls-key.pem /tmp/db-tls/key.pem
 cp /fixtures/tls-cert.pem /tmp/db-tls/cert.pem
 chmod 600 /tmp/db-tls/key.pem
 chown -R postgres:postgres /tmp/db-tls
fi
exec /usr/local/bin/docker-entrypoint.sh "$@"
''')
    specification={'services': {
        'postgres': {'image':'postgres:17.11-bookworm','environment':{'POSTGRES_DB':'pgxio','POSTGRES_PASSWORD':password,'PGXIO_WEB_PASSWORD':password},
            'entrypoint':['sh','/fixtures/db-entrypoint.sh'], 'command':postgres_command,
            'volumes':[f'{ROOT}/sql/example.sql:/example.sql:ro',f'{ROOT}/deploy/init-db.sh:/docker-entrypoint-initdb.d/init.sh:ro',f'{runtime}:/fixtures:ro'],
            'healthcheck':{'test':['CMD-SHELL','pg_isready -U postgres -d pgxio'],'interval':'1s','timeout':'2s','retries':30},
            'networks':{'backend':{'ipv4_address':'172.29.89.2'}}},
        'api': {'image':args.image,'environment':{'PGXIO_DATABASE_URL':database_url,'PGXIO_DEV_SECRET':secret},
            'volumes':[f'{runtime}:/etc/pgxio:ro',f'{runtime}/nginx.conf:/etc/nginx/nginx.conf:ro'],
            'ports':['127.0.0.1:18090:8080','127.0.0.1:18490:8443'],
            'depends_on':{'postgres':{'condition':'service_healthy'}},'networks':['backend'],
            'read_only':True,'tmpfs':['/tmp:uid=10001,gid=10001,mode=1770'], 'cap_drop':['ALL'],
            'security_opt':['no-new-privileges:true']}},
        'networks':{'backend':{'ipam':{'config':[{'subnet':'172.29.89.0/24'}]}}}}
    compose_path=directory/'compose.json'; compose_path.touch(mode=0o600,exist_ok=True);compose_path.chmod(0o600)
    compose_path.write_text(json.dumps(specification))
    outcomes=[]
    try:
        run(command+['up','--detach','--wait'])
        run(command+['exec','-T','postgres','psql','-U','postgres','-d','pgxio','-v','ON_ERROR_STOP=1'], input=(ROOT/'tests/performance.sql').read_bytes())
        suites=['integration','performance'] if args.suite=='all' else [args.suite]
        for suite in suites:
            if suite == 'performance' and config['pool_size'] != 1:
                config['pool_size']=1
                (runtime/'pgxio.json').write_text(json.dumps(config))
                run(command+['up','--detach','--force-recreate','--no-deps','api'])
            result=subprocess.run([sys.executable,str(ROOT/'tests'/('integration.py' if suite=='integration' else 'performance.py'))],env=env,cwd=ROOT,text=True,capture_output=True)
            (directory/(suite+'.log')).write_text(result.stdout+result.stderr)
            print(suite+': '+('PASS' if result.returncode==0 else 'FAIL'),flush=True)
            if result.returncode: print((result.stdout+result.stderr)[-7000:],flush=True)
            outcomes.append({'suite':suite,'passed':result.returncode==0})
            if result.returncode: break
    finally:
        logs=run(command+['logs','--no-color'],text=True).stdout
        # Logs may contain transient connection diagnostics; keep them private.
        path=directory/'containers.log';path.touch(mode=0o600,exist_ok=True);path.chmod(0o600);path.write_text(logs)
        run(command+['down','--volumes','--remove-orphans'])
    (directory/'outcomes.json').write_text(json.dumps({'profile':args.profile,'production':args.production,'image':args.image,'suites':outcomes},indent=2))
    return 0 if outcomes and all(outcome['passed'] for outcome in outcomes) else 1

if __name__=='__main__': sys.exit(main())

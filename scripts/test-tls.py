#!/usr/bin/env python3
"""Create local-only TLS fixtures; run HTTP/WSS suite through Nginx TLS itself."""
import os
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parent.parent
from nginx_config import read_settings, render
p=argparse.ArgumentParser(description=__doc__)
p.add_argument("--production",action="store_true",help="also test RS256 JWKS and verify-full PostgreSQL TLS")
args=p.parse_args()
fixture=root/".pgxio/test-tls"
fixture.mkdir(parents=True,exist_ok=True)
subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-keyout",str(fixture/"key.pem"),"-out",str(fixture/"cert.pem"),"-subj","/CN=localhost","-addext","subjectAltName=DNS:localhost,DNS:postgres,IP:127.0.0.1"],check=True,capture_output=True)
# Local test private key only; UID 10001 must read it through the bind mount.
(fixture/"key.pem").chmod(0o644)
conf=(root/"deploy/nginx.conf").read_text().replace("listen 8080;", "listen 8080;\n        listen 8443 ssl;\n        ssl_certificate /etc/pgxio-tls/cert.pem;\n        ssl_certificate_key /etc/pgxio-tls/key.pem;")
(fixture/"nginx.conf").write_text(conf)
command=["docker","compose","-f","compose.yaml","-f","tests/compose.tls.yaml"]
def sql(statement):
    subprocess.run(["docker","compose","exec","-T","postgres","psql","-U","postgres","-d","pgxio","-v","ON_ERROR_STOP=1"],input=statement,text=True,cwd=root,check=True,capture_output=True)
env=dict(os.environ,PGXIO_TEST_URL="https://localhost:8443",SSL_CERT_FILE=str(fixture/"cert.pem"))
try:
    if args.production:
        # This fixture changes ONLY the included local development database.
        for name in ("key.pem","cert.pem"):
            subprocess.run(["docker","compose","cp",str(fixture/name),"postgres:/tmp/pgxio-"+name],cwd=root,check=True,capture_output=True)
        subprocess.run(["docker","compose","exec","-T","--user","root","postgres","chown","postgres:postgres","/tmp/pgxio-key.pem"],cwd=root,check=True)
        subprocess.run(["docker","compose","exec","-T","--user","root","postgres","chmod","600","/tmp/pgxio-key.pem"],cwd=root,check=True)
        sql("ALTER SYSTEM SET ssl_cert_file='/tmp/pgxio-cert.pem'; ALTER SYSTEM SET ssl_key_file='/tmp/pgxio-key.pem'; ALTER SYSTEM SET ssl='on'; SELECT pg_reload_conf();")
        private=fixture/"jwt-key.pem"
        subprocess.run(["openssl","genpkey","-algorithm","RSA","-pkeyopt","rsa_keygen_bits:2048","-pkeyopt","rsa_keygen_pubexp:65537","-out",str(private)],check=True,capture_output=True)
        modulus=subprocess.check_output(["openssl","rsa","-in",str(private),"-noout","-modulus"],text=True).strip().split("=",1)[1]
        enc=lambda value:base64.urlsafe_b64encode(value).rstrip(b"=").decode()
        (fixture/"jwks.json").write_text(json.dumps({"keys":[{"kty":"RSA","kid":"local-test","alg":"RS256","use":"sig","n":enc(bytes.fromhex(modulus)),"e":"AQAB"}]}))
        config,routes=read_settings(root/"examples/pgxio.conf")
        config.update(development=False,jwks_file="/etc/pgxio-tls/jwks.json")
        (fixture/"pgxio.conf").write_text(render(config,routes))
        secrets=dict(line.split("=",1) for line in (root/".env").read_text().splitlines() if line and not line.startswith("#"))
        url=f"postgresql://web:{secrets['PGXIO_WEB_PASSWORD']}@postgres/pgxio?hostaddr=172.29.87.2&sslmode=verify-full&sslrootcert=/etc/pgxio-tls/cert.pem"
        override={"services":{"api":{"environment":{"PGXIO_DATABASE_URL":url},"volumes":["./.pgxio/test-tls/pgxio.conf:/etc/pgxio/pgxio.conf:ro"]}}}
        override_path=fixture/"production-compose.json"
        override_path.touch(mode=0o600,exist_ok=True)
        override_path.write_text(json.dumps(override))
        command += ["-f",str(override_path)]
        env["PGXIO_TEST_RSA_KEY"]=str(private)
    subprocess.run(command+["up","-d","--no-build"],cwd=root,check=True)
    subprocess.run([sys.executable,"tests/integration.py"],cwd=root,env=env,check=True)
finally:
    if args.production:
        sql("ALTER SYSTEM RESET ssl; ALTER SYSTEM RESET ssl_cert_file; ALTER SYSTEM RESET ssl_key_file; SELECT pg_reload_conf();")
    subprocess.run(["docker","compose","up","-d","--no-build"],cwd=root,check=True)

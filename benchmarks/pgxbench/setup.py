import re
import json
import sys
import os
from pathlib import Path
import secrets
import ipaddress
import shutil
import time
import subprocess
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote

from .common import ROOT, REPO, TARGETS, command, compose, dump, encode, load_settings
from .fixtures import contract, initialization, seed_sql
sys.path.insert(0, str(REPO))
from scripts.nginx_config import render as render_nginx, schemas

# Explicit release pins; `lock` records pulled immutable image IDs/digests too.
VERSIONS = {"postgres": "postgres:17.11-bookworm", "postgrest": "postgrest/postgrest:v14.17",
            "openresty": "openresty/openresty:1.27.1.2-0-bookworm-fat",
            "nginx": "nginx:1.28.0-bookworm", "node": "node:22.20.0-bookworm-slim",
            "golang": "golang:1.25.1-bookworm", "debian": "debian:bookworm-slim",
            "supabase_postgres": "supabase/postgres:17.6.1.136",
            "realtime": "supabase/realtime:v2.134.10"}


def prepare(workdir):
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    workdir.chmod(0o700)
    if (workdir / "settings.json").exists():
        return load_settings(workdir)
    runtime = workdir / "runtime"
    runtime.mkdir(exist_ok=True)
    private = workdir / "jwt-private.pem"
    command(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
             "-pkeyopt", "rsa_keygen_pubexp:65537", "-out", private])
    private.chmod(0o600)
    command(["openssl", "pkey", "-in", private, "-pubout", "-out", runtime / "jwt-public.pem"])
    modulus = command(["openssl", "rsa", "-in", private, "-noout", "-modulus"]).split("=", 1)[1]
    dump(runtime / "jwks.json", {"keys": [{"kty": "RSA", "kid": "pgxbench", "alg": "RS256",
                                          "use": "sig", "n": encode(bytes.fromhex(modulus)), "e": "AQAB"}]})
    command(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
             "-keyout", runtime / "tls-key.pem", "-out", runtime / "tls-cert.pem",
             "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"])
    # Disposable local TLS key, readable by each container's unprivileged UID.
    # The JWT SIGNING key stays outside the mounted runtime directory, mode 0600.
    (runtime / "tls-key.pem").chmod(0o644)
    settings = {"versions": VERSIONS, "volume_prefix": "pgxbench-" + secrets.token_hex(6), "admin_password": secrets.token_hex(32),
                "web_password": secrets.token_hex(32), "realtime_secret": secrets.token_hex(32),
                "secret_key_base": secrets.token_hex(64), "target": "pgxio", "cpus": 1,
                "workers": 1, "pool_mode": "fixed", "pool_total": 8, "pool_per_worker": 8,
                "memory_mib": 0, "max_websockets": 1024, "cpuset": "", "db_cpuset": "",
                "cgroup_parent": "", "cgroup_path": "", "rows": 10000, "tenants": 100,
                "payload_bytes": 128, "db_memory_mib": 0, "external_database": False}
    settings.update(http_port=18080, https_port=18443, admin_port=18081, bind_address="127.0.0.1", frontend="http")
    (runtime / "init.sql").write_text(initialization(settings["web_password"]))
    (runtime / "init.sql").chmod(0o644)
    dump(runtime / "contract.json", contract())
    dump(workdir / "settings.json", settings, private=True)
    return settings


def nginx_config(workers, sockets, baseline=False):
    text = (REPO / "deploy" / "nginx.conf").read_text()
    text = text.replace("worker_processes 1;", f"worker_processes {workers};")
    text = text.replace("worker_connections 4096", f"worker_connections {max(4096, sockets + 2048)}")
    text = text.replace("include /etc/pgxio/pgxio.conf;", "pgxio_config /bench/pgxio.json;" if baseline else "include /bench/pgxio.conf;")
    text = text.replace("listen 8080;", "listen 8080;\n        listen 8443 ssl;\n"
                        "        ssl_certificate /bench/tls-cert.pem;\n"
                        "        ssl_certificate_key /bench/tls-key.pem;\n"
                        "        ssl_protocols TLSv1.3;")
    return text


def proxy_config(supabase, workers, sockets):
    ws = """
        location /realtime/v1/ {
            proxy_pass http://realtime:4000/socket/;
            proxy_http_version 1.1;
            proxy_set_header Host realtime-dev.localhost;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 3600s;
        }
""" if supabase else ""
    return f"""worker_processes {workers};
pid /tmp/nginx.pid;
events {{ worker_connections {max(4096, sockets * 2 + 2048)}; }}
http {{
    access_log off;
    client_max_body_size 64k;
    server {{
        listen 8080;
        listen 8443 ssl;
        ssl_certificate /bench/tls-cert.pem;
        ssl_certificate_key /bench/tls-key.pem;
        ssl_protocols TLSv1.3;
        location = /readyz {{ proxy_pass http://postgrest:3001/ready; }}
        location / {{ proxy_pass http://postgrest:3000; proxy_http_version 1.1; }}
        {ws}
    }}
}}
"""


def render(workdir, updates):
    workdir = Path(workdir).resolve()
    settings = prepare(workdir)
    settings.update({k: v for k, v in updates.items() if v is not None})
    # Existing saved experiments retain their original TLS-enabled topology.
    settings.setdefault("frontend", "tls")
    if settings["frontend"] not in ("http", "tls"):
        raise ValueError("frontend must be http or tls")
    frontend_tls = settings["frontend"] == "tls"
    settings.setdefault('database_tls', 'verify-full')
    if settings['database_tls'] not in ('verify-full', 'disable'):
        raise ValueError('database_tls must be verify-full or disable')
    database_encrypted = settings['database_tls'] == 'verify-full'
    settings.setdefault('auth_mode', 'jwt')
    settings.setdefault('auth_fixture', 'tenant')
    settings.setdefault('jwt_cache', 'default')
    if settings['auth_mode'] not in ('jwt', 'none') or settings['auth_fixture'] not in ('tenant', 'public') or settings['jwt_cache'] not in ('default', 'off'):
        raise ValueError('invalid authentication profile')
    if settings['auth_mode'] == 'none' and (settings['auth_fixture'] != 'public' or settings['target'] not in ('pgxio', 'postgrest')):
        raise ValueError('no-JWT mode requires public fixtures and currently supports pgxio/postgrest REST only')
    if updates.get("external_db_env"):
        value = os.environ[updates["external_db_env"]]
        parsed = urlsplit(value)
        if parsed.scheme not in ("postgres", "postgresql") or parsed.username != "web" or parsed.path != "/pgxbench":
            raise ValueError("external DB must use the restricted web role and dedicated pgxbench database")
        if "hostaddr" not in dict(parse_qsl(parsed.query)):
            raise ValueError("external libpq URL needs hostaddr resolved outside the pgxio worker")
        settings.update(external_database=True, external_database_url=value)
    if not database_encrypted and settings.get('external_database'):
        raise ValueError('database_tls=disable is only supported for the isolated local database')
    target = settings["target"]
    if target not in TARGETS:
        raise ValueError("unknown target")
    cpus = settings["cpus"]
    workers = settings["workers"]
    if min(cpus, workers, settings["max_websockets"]) < 1 or cpus > 256 or workers > 256:
        raise ValueError("invalid CPU/worker/socket limits")
    if settings["pool_mode"] == "fixed":
        if 8 % workers:
            raise ValueError("fixed total pool of 8 must divide evenly across workers (use 1/2/4/8)")
        pool = 8 // workers
    else:
        pool = 4
    settings.update(pool_per_worker=pool, pool_total=pool * workers)
    memory = settings["memory_mib"]
    if memory < 0 or settings["db_memory_mib"] < 0:
        raise ValueError("memory limits cannot be negative")
    if (target == "supabase" or (target == "postgrest" and frontend_tls)) and memory and not settings["cgroup_parent"]:
        raise ValueError("multi-service RAM budgets require --cgroup-parent and --cgroup-path; see README")
    if settings["cgroup_parent"] and not settings["cgroup_path"]:
        raise ValueError("--cgroup-parent requires the collector/enforcer's Linux --cgroup-path")
    versions = settings["versions"]
    runtime = workdir / "runtime"
    database_tls = workdir / "database-tls"
    dump(runtime / 'contract.json', contract())
    if not (database_tls / "key.pem").exists():
        database_tls.mkdir(mode=0o700, exist_ok=True)
        command(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-pkeyopt", "ec_param_enc:named_curve", "-nodes", "-days", "365",
                 "-keyout", database_tls / "key.pem", "-out", database_tls / "cert.pem", "-subj", "/CN=postgres",
                 "-addext", "subjectAltName=DNS:postgres,IP:172.29.88.2"])
        (database_tls / "key.pem").chmod(0o600)
    shutil.copyfile(database_tls / "cert.pem", runtime / "database-ca.pem")
    (runtime / "db-entrypoint.sh").write_text('''#!/bin/sh
set -eu
mkdir -p /tmp/pgxbench-tls
cp /db-tls/key.pem /tmp/pgxbench-tls/key.pem
cp /db-tls/cert.pem /tmp/pgxbench-tls/cert.pem
chmod 600 /tmp/pgxbench-tls/key.pem
if [ "$(id -u)" = 0 ]; then chown -R postgres /tmp/pgxbench-tls; fi
exec /usr/local/bin/docker-entrypoint.sh "$@"
''')
    init = initialization(settings["web_password"])
    if target == "supabase":
        init += ("\nCREATE SCHEMA IF NOT EXISTS _realtime;\n"
                 "ALTER SCHEMA _realtime OWNER TO supabase_admin;\n"
                 f"ALTER ROLE supabase_admin PASSWORD '{settings['admin_password']}';\n")
    (runtime / "init.sql").write_text(init)
    (runtime / "init.sql").chmod(0o644)
    module_config = {"contract": "/bench/contract-v2.json", "issuer": "pgxbench",
         "audience": "pgxbench", "development": not database_encrypted, "jwks_file": "/bench/jwks.json",
         "pool_size": pool, "max_websockets": settings["max_websockets"], "origins": []}
    module_options = settings.get('module_options', {})
    allowed_options = {'libpq_buf','validate_rest_input','validate_output','db_prepared_statements','db_pipeline','pool_acquisition_timeout_ms','pool_queue_limit','jwt_cache_max_entries','jwt_cache_max_bytes'}
    if not isinstance(module_options, dict) or set(module_options) - allowed_options:
        raise ValueError('unsupported pgxio module options')
    if 'libpq_buf' in module_options and module_options['libpq_buf'] not in ('copy','borrowed','auto'):
        raise ValueError('libpq_buf must be copy, borrowed or auto')
    module_config.update(module_options)
    auth_build = settings['auth_fixture'] == 'public' and target == 'pgxio'
    if auth_build and settings['auth_mode'] == 'none':
        module_config['benchmark_no_jwt'] = True
    config_format = settings.get('config_format', 'auto')
    if config_format == 'auto':
        config_format = 'nginx-v1'
        if target == 'pgxio' and settings.get('pgxio_image'):
            image = json.loads(command(['docker', 'image', 'inspect', settings['pgxio_image']]))[0]
            config_format = (image.get('Config', {}).get('Labels') or {}).get('io.pgxio.config-format', 'legacy-json')
    if config_format not in ('legacy-json', 'nginx-v1'):
        raise ValueError('unsupported pgxio config format')
    settings['effective_config_format'] = config_format
    if config_format == 'legacy-json':
        if 'validate_output' in module_config:
            raise ValueError('legacy images do not support optional output validation')
        module_config['contract'] = '/bench/contract.json'
        dump(runtime / 'pgxio.json', module_config)
    else:
        module_config.setdefault('libpq_buf', 'copy')
        if settings.get('contract_enabled', True) is False:
            module_config.pop('contract')
        dump(runtime / 'contract-v2.json', schemas(contract()))
        (runtime / 'pgxio.conf').write_text(render_nginx(module_config, contract()))
    nginx_profile = settings.get('nginx_profile', 'baseline')
    if nginx_profile not in ('baseline','keepalive-10000','reuseport'):
        raise ValueError('unknown Nginx experiment profile')
    nginx = nginx_config(workers, settings["max_websockets"], baseline=config_format == "legacy-json")
    if nginx_profile == 'keepalive-10000':
        nginx = nginx.replace('    keepalive_timeout 30s;', '    keepalive_timeout 30s;\n    keepalive_requests 10000;')
    elif nginx_profile == 'reuseport':
        nginx = nginx.replace('listen 8080;', 'listen 8080 reuseport;').replace('listen 8443 ssl;', 'listen 8443 ssl reuseport;')
    (runtime / "nginx.conf").write_text(nginx)
    (runtime / "proxy.conf").write_text(proxy_config(target == "supabase", workers, settings["max_websockets"]))
    (runtime / "openresty.conf").write_text((ROOT / "reference" / "openresty" / "nginx.conf").read_text()
        .replace("WORKERS_PLACEHOLDER", str(workers))
        .replace("CONNECTIONS_PLACEHOLDER", str(max(4096, settings["max_websockets"] + 2048))))
    if not frontend_tls:
        for name in ("nginx.conf", "openresty.conf", "proxy.conf"):
            path = runtime / name
            path.write_text('\n'.join(line for line in path.read_text().splitlines()
                           if not any(key in line for key in ("listen 8443 ssl", "ssl_certificate", "ssl_protocols"))) + '\n')
    db = {"image": versions["supabase_postgres" if target == "supabase" else "postgres"],
          "environment": {"POSTGRES_PASSWORD": settings["admin_password"], "POSTGRES_DB": "pgxbench"},
          "volumes": [f"db-{('supabase' if target == 'supabase' else 'standard')}:/var/lib/postgresql/data",
                      f"{runtime}/init.sql:/docker-entrypoint-initdb.d/zz-pgxbench.sql:ro",
                      f"{database_tls}:/db-tls:ro", f"{runtime}/db-entrypoint.sh:/bench-db-entrypoint.sh:ro"],
          "entrypoint": ["/bin/sh", "/bench-db-entrypoint.sh"],
          "command": ["postgres", "-c", "wal_level=logical", "-c", "max_replication_slots=20",
                      "-c", "listen_addresses=*",
                      "-c", "max_wal_senders=20", "-c", "shared_buffers=32MB", "-c", "max_connections=200",
                      "-c", "track_functions=all"],
          "healthcheck": {"test": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U postgres -d pgxbench"],
                          "interval": "2s", "timeout": "2s", "retries": 60},
          "networks": {"bench": {"ipv4_address": "172.29.88.2"}}, "labels": {"pgxbench.role": "database"}}
    if settings["db_cpuset"]:
        db["cpuset"] = settings["db_cpuset"]
    if settings["db_memory_mib"]:
        db.update(mem_limit=f"{settings['db_memory_mib']}m", memswap_limit=f"{settings['db_memory_mib']}m")
    database_url = (f"postgresql://web:{settings['web_password']}@postgres/pgxbench"
                    "?hostaddr=172.29.88.2&sslmode=verify-full&sslrootcert=/bench/database-ca.pem")
    db["command"] += ["-c", "ssl=on", "-c", "ssl_cert_file=/tmp/pgxbench-tls/cert.pem",
                       "-c", "ssl_key_file=/tmp/pgxbench-tls/key.pem", "-c", "ssl_min_protocol_version=TLSv1.3"]
    if settings["external_database"]:
        database_url = settings["external_database_url"]
    reference_url = (f"postgresql://web:{settings['web_password']}@172.29.88.2:5432/pgxbench"
                     "?sslmode=verify-full&sslrootcert=/bench/database-ca.pem")
    if not database_encrypted:
        database_url = (f"postgresql://web:{settings['web_password']}@postgres/pgxbench"
                        "?hostaddr=172.29.88.2&sslmode=disable")
        reference_url = (f"postgresql://web:{settings['web_password']}@172.29.88.2:5432/pgxbench"
                         "?sslmode=disable")
        db['command'] += ['-c', 'ssl=off']
    if settings["external_database"]:
        parsed = urlsplit(database_url)
        reference_url = urlunsplit(parsed._replace(query=urlencode([(k,v) for k,v in parse_qsl(parsed.query) if k != "hostaddr"])))
    parsed = urlsplit(reference_url)
    options = dict(parse_qsl(parsed.query))
    ssl_mode = options.get("sslmode", "prefer")
    dump(runtime / "reference.json", {"pool": pool, "max_websockets": settings["max_websockets"], "database": {
        "host": parsed.hostname, "port": str(parsed.port or 5432), "database": parsed.path.lstrip("/"),
        "user": unquote(parsed.username), "password": unquote(parsed.password or ""),
        "ssl": ssl_mode not in ("disable", "allow", "prefer"), "ssl_required": ssl_mode not in ("disable", "allow", "prefer"),
        "ssl_verify": ssl_mode in ("verify-ca", "verify-full")}})
    trust = options.get("sslrootcert", "/etc/ssl/certs/ca-certificates.crt")
    if any(c in trust for c in (";", "\n", "\r", " ")):
        raise ValueError("unsupported SSL CA path")
    openresty_conf = (runtime / "openresty.conf").read_text().replace("    access_log off;",
        f"    access_log off;\n    resolver 127.0.0.11 ipv6=on;\n    lua_ssl_trusted_certificate {trust};")
    (runtime / "openresty.conf").write_text(openresty_conf)
    env = {"DATABASE_URL": reference_url, "PGXIO_DATABASE_URL": database_url,
           "POOL_SIZE": str(pool if target not in ("go",) else pool * workers),
           "WORKERS": str(workers), "GOMAXPROCS": str(cpus),
           "MAX_WEBSOCKETS": str(settings["max_websockets"] if target != "go" else settings["max_websockets"] * workers),
           "FRONTEND_TLS": "1" if frontend_tls else "0"}
    if not database_encrypted:
        # Development mode permits plaintext libpq; measured tokens still use RS256/JWKS.
        env['PGXIO_DEV_SECRET'] = settings['realtime_secret']

    def api(extra, name):
        value = {"environment": dict(env), "volumes": [f"{runtime}:/bench:ro"],
                 "depends_on": {} if settings["external_database"] else {"postgres": {"condition": "service_healthy"}},
                 "networks": ["bench"], "cpus": cpus,
                 "ulimits": {"nofile": {"soft": max(65536, settings["max_websockets"] * 4 + 4096),
                                        "hard": max(65536, settings["max_websockets"] * 4 + 4096)}},
                 "labels": {"pgxbench.role": "api", "pgxbench.target": target}}
        if settings["cpuset"]:
            value["cpuset"] = settings["cpuset"]
        if settings["cgroup_parent"]:
            value["cgroup_parent"] = settings["cgroup_parent"]
        elif memory:
            value.update(mem_limit=f"{memory}m", memswap_limit=f"{memory}m")
        value.update(extra)
        return value

    services = {"postgres": db}
    ports = [f"{settings.get('bind_address', '127.0.0.1')}:{settings.get('http_port',18080)}:8080",
             f"{settings.get('bind_address', '127.0.0.1')}:{settings.get('https_port',18443)}:8443"]
    if not frontend_tls:
        ports = ports[:1]
    if target == "pgxio":
        services["pgxio"] = api({"build": {"context": str(REPO), "args": {"CARGO_FEATURES": "benchmark-auth" if auth_build else ""}}, "image": "pgxio:bench-auth" if auth_build else "pgxio:bench",
            "command": ["-c", "/bench/nginx.conf", "-g", "daemon off;"], "ports": ports}, target)
        if settings.get('pgxio_image'):
            services['pgxio']['image'] = settings['pgxio_image']
            services['pgxio'].pop('build', None)
    elif target in ("node", "go", "openresty"):
        buildargs = {"NODE_IMAGE": versions["node"]} if target == "node" else (
            {"GO_IMAGE": versions["golang"], "RUNTIME_IMAGE": versions["debian"]} if target == "go"
            else {"OPENRESTY_IMAGE": versions["openresty"]})
        services[target] = api({"build": {"context": str(ROOT / "reference" / target), "args": buildargs},
                                "image": "pgxbench-" + target, "ports": ports}, target)
    else:
        jwks = (runtime / "jwks.json").read_text()
        services["postgrest"] = api({"image": versions["postgrest"], "environment": {
            "PGRST_DB_URI": database_url, "PGRST_DB_SCHEMAS": "web", "PGRST_JWT_SECRET": jwks,
            "PGRST_JWT_AUD": "pgxbench", "PGRST_DB_POOL": str(pool * workers),
            "PGRST_ADMIN_SERVER_PORT": "3001", "PGRST_SERVER_HOST": "0.0.0.0",
            "PGRST_ADMIN_SERVER_HOST": "0.0.0.0", "PGRST_SERVER_PORT": "3000",
            "PGRST_DB_POOL_ACQUISITION_TIMEOUT": "1", "PGRST_LOG_LEVEL": "crit",
            "GHCRTS": f"-N{cpus}"}}, "postgrest")
        services["proxy"] = api({"image": versions["nginx"], "ports": ports,
             "command": ["nginx", "-c", "/bench/proxy.conf", "-g", "daemon off;"],
             "depends_on": {"postgrest": {"condition": "service_started"}}}, "proxy")
        services['postgrest']['environment']['PGRST_JWT_CACHE_MAX_ENTRIES'] = '0' if settings['jwt_cache'] == 'off' else '1000'
        if settings['auth_mode'] == 'none':
            services['postgrest']['environment']['PGRST_DB_ANON_ROLE'] = 'web'
        if target == "supabase":
            services["proxy"]["depends_on"]["realtime"] = {"condition": "service_started"}
            services["realtime"] = api({"image": versions["realtime"], "hostname": "realtime-dev.localhost",
              "command": ["sh", "-c", "touch /tmp/pgxbench-serving; exec /app/bin/server"],
              "environment": {"PORT": "4000", "DB_HOST": "postgres", "DB_PORT": "5432",
                "DB_USER": "supabase_admin", "DB_PASSWORD": settings["admin_password"], "DB_NAME": "pgxbench",
                "DB_AFTER_CONNECT_QUERY": "SET search_path TO _realtime", "DB_ENC_KEY": "supabaserealtime",
                "API_JWT_SECRET": settings["realtime_secret"], "API_JWT_JWKS": jwks,
                "METRICS_JWT_SECRET": settings["realtime_secret"],
                "SECRET_KEY_BASE": settings["secret_key_base"], "ERL_AFLAGS": f"-proto_dist inet_tcp +S {cpus}:{cpus}",
                "DNS_NODES": "''", "RLIMIT_NOFILE": str(max(65536, settings["max_websockets"] * 4)),
                "APP_NAME": "realtime", "SEED_SELF_HOST": "true", "RUN_JANITOR": "true",
                "DISABLE_HEALTHCHECK_LOGGING": "true"}}, "realtime")
            # Official database bootstrap owns this reserved role; set its generated password.
            db["environment"].update(POSTGRES_USER="supabase_admin", POSTGRES_HOST="/var/run/postgresql",
                POSTGRES_PORT="5432", PGPORT="5432", PGDATABASE="pgxbench")
        elif not frontend_tls:
            services.pop("proxy")
            services["postgrest"]["ports"] = [
                f"{settings.get('bind_address','127.0.0.1')}:{settings.get('http_port',18080)}:3000",
                f"127.0.0.1:{settings.get('admin_port',18081)}:3001"]
    document = {"name": "pgxbench", "services": services,
                "networks": {"bench": {"ipam": {"config": [{"subnet": "172.29.88.0/24"}]}}},
                "volumes": {"db-standard": {}, "db-supabase": {}}}
    if settings.get("volume_prefix"):
        prefix = settings["volume_prefix"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", prefix):
            raise ValueError("invalid benchmark volume prefix")
        document["volumes"] = {key: {"name": prefix + "-" + key} for key in document["volumes"]}
    if settings["external_database"]:
        services.pop("postgres")
        if target == "supabase":
            parsed = urlsplit(database_url)
            admin = urlsplit(os.environ["PGXBENCH_ADMIN_URL"])
            services["realtime"]["environment"].update(DB_HOST=parsed.hostname, DB_PORT=str(parsed.port or 5432),
                DB_USER=unquote(admin.username), DB_PASSWORD=unquote(admin.password or ""), DB_NAME="pgxbench",
                DB_SSL="true" if dict(parse_qsl(parsed.query)).get("sslmode") != "disable" else "false")
    dump(workdir / "compose.json", document, private=True)
    dump(workdir / "settings.json", settings, private=True)
    return settings


def sql(workdir, statement):
    if load_settings(workdir)["external_database"]:
        admin = os.environ["PGXBENCH_ADMIN_URL"]
        if urlsplit(admin).path != "/pgxbench":
            raise ValueError("PGXBENCH_ADMIN_URL must address the disposable pgxbench database")
        return command(["psql", "-X", "--set", "ON_ERROR_STOP=1", "--quiet"],
                       input=statement, env=dict(os.environ, PGDATABASE=admin))
    role = "supabase_admin" if load_settings(workdir)["target"] == "supabase" else "postgres"
    return compose(workdir, "exec", "-T", "postgres", "psql", "-X", "-U", role, "-d", "pgxbench",
                   "--set", "ON_ERROR_STOP=1", "--quiet", "--tuples-only", "--no-align", input=statement)


def seed(workdir, rows, tenants, payload_bytes, supabase=False):
    sql(workdir, (ROOT / 'sql' / 'public.sql').read_text())
    statement = seed_sql(rows, tenants, payload_bytes)
    sql(workdir, statement)
    sql(workdir, "SELECT pg_notify('pgrst','reload schema');")
    if supabase:
        deadline = time.monotonic() + 120
        # The official entrypoint briefly serves HTTP during its seed eval.
        # Our final command writes this marker only after that process exits.
        while True:
            try:
                compose(workdir, "exec", "-T", "realtime", "test", "-f", "/tmp/pgxbench-serving")
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Realtime bootstrap did not finish")
                time.sleep(1)
        while sql(workdir, "SELECT to_regclass('realtime.messages') IS NOT NULL;").strip() != "t":
            if time.monotonic() >= deadline:
                raise RuntimeError("Realtime tenant migrations did not finish")
            time.sleep(1)
        sql(workdir, (ROOT / "sql" / "supabase.sql").read_text())
    settings = load_settings(workdir)
    settings.update(rows=rows, tenants=tenants, payload_bytes=payload_bytes)
    dump(Path(workdir) / "settings.json", settings, private=True)
    return {"rows": rows, "tenants": tenants, "payload_bytes": payload_bytes}


def lock(workdir):
    settings = load_settings(workdir)
    ids = compose(workdir, "ps", "--all", "--quiet").splitlines()
    records = []
    for container in ids:
        info = json.loads(command(["docker", "inspect", container]))[0]
        image = json.loads(command(["docker", "image", "inspect", info["Image"]]))[0]
        records.append({"service": info["Config"]["Labels"].get("com.docker.compose.service"),
                        "image_id": info["Image"], "digests": image.get("RepoDigests", []),
                        "architecture": image.get("Architecture"), "os": image.get("Os"),
                        "output_validation_default": (image.get("Config", {}).get("Labels") or {}).get("io.pgxio.output-validation", "schema"),
                        "config_format": (image.get("Config", {}).get("Labels") or {}).get("io.pgxio.config-format", "legacy-json"),
                        "rest_input_validation_default": (image.get('Config', {}).get('Labels') or {}).get('io.pgxio.rest-input-validation', 'schema'),
                        "borrowed_libpq": (image.get('Config', {}).get('Labels') or {}).get('io.pgxio.borrowed-libpq', '0'),
                        "libpq_base": (image.get('Config', {}).get('Labels') or {}).get('io.pgxio.libpq-base')})
    result = {"images": records, "requested_versions": settings["versions"]}
    dump(Path(workdir) / "images.lock.json", result)
    return result


def certificate(workdir, hostname=None, database=False):
    if database:
        folder = Path(workdir) / "database-tls"
        folder.mkdir(mode=0o700, exist_ok=True)
        command(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-pkeyopt", "ec_param_enc:named_curve", "-nodes", "-days", "365",
                 "-keyout", folder / "key.pem", "-out", folder / "cert.pem", "-subj", "/CN=postgres",
                 "-addext", "subjectAltName=DNS:postgres,IP:172.29.88.2"])
        (folder / "key.pem").chmod(0o600)
        shutil.copyfile(folder / "cert.pem", Path(workdir) / "runtime" / "database-ca.pem")
        return {"database_certificate": "ECDSA P-256", "restart_required": True}
    if not hostname or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:" for c in hostname):
        raise ValueError("provide a DNS name or IP address")
    try:
        ipaddress.ip_address(hostname)
        extra = "IP:" + hostname
    except ValueError:
        extra = "DNS:" + hostname
    runtime = Path(workdir) / "runtime"
    command(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
             "-keyout", runtime / "tls-key.pem", "-out", runtime / "tls-cert.pem", "-subj", "/CN=" + hostname,
             "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1," + extra])
    (runtime / "tls-key.pem").chmod(0o644)
    return {"hostname": hostname, "restart_required": True, "reexport_client_CA": True}

import argparse
import asyncio
import json
import os
import math
from pathlib import Path
import shutil
import subprocess
import sys

from .common import REPO, TARGETS, compose, dump, load_settings, public_settings, token
from .setup import lock, prepare, render, seed


def positive(value):
    result = float(value)
    if result <= 0 or not math.isfinite(result):
        raise argparse.ArgumentTypeError("must be positive")
    return result


def comma_ints(value):
    try:
        values = [int(part) for part in value.split(",")]
        if not values or min(values) < 1:
            raise ValueError()
        return values
    except ValueError:
        raise argparse.ArgumentTypeError("expected comma-separated positive integers")


def parser():
    p = argparse.ArgumentParser(description="Isolated pgxio competitor benchmarks. Full guide: benchmarks/README.md")
    p.add_argument("--workdir", type=Path, default=REPO / "artifacts" / "bench")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="create disposable keys/configuration; preserves existing settings")
    cert = sub.add_parser("certificate", help="issue disposable TLS certificate for the private benchmark host")
    cert_group = cert.add_mutually_exclusive_group(required=True)
    cert_group.add_argument("--hostname")
    cert_group.add_argument("--database", action="store_true")
    config = sub.add_parser("render", help="generate isolated Compose/config files without starting containers")
    config.add_argument("--target", choices=TARGETS)
    config.add_argument("--frontend", choices=("http", "tls"), help="http: backend baseline; tls: separate TLS-enabled profile")
    config.add_argument('--database-tls', choices=('verify-full', 'disable'), help='isolated database transport; defaults to verify-full')
    config.add_argument('--auth-mode', choices=('jwt', 'none'))
    config.add_argument('--auth-fixture', choices=('tenant', 'public'), help='public: identical SQL/data for paired authentication-cost tests')
    config.add_argument('--jwt-cache', choices=('default', 'off'), help='PostgREST JWT verification cache; off for matched verification cost')
    config.add_argument('--pgxio-image', help='prebuilt immutable stage image; disables automatic rebuilding')
    config.add_argument("--config-format", choices=("auto", "nginx-v1", "legacy-json"), default="auto")
    config.add_argument('--module-options', type=json.loads, help='JSON object of pgxio performance options; omit for legacy images')
    config.add_argument('--nginx-profile', choices=('baseline','keepalive-10000','reuseport'))
    config.add_argument("--cpus", type=int)
    config.add_argument("--workers", type=int)
    config.add_argument("--pool-mode", choices=("fixed", "growing"))
    config.add_argument("--memory-mib", type=int)
    config.add_argument("--db-memory-mib", type=int)
    config.add_argument("--max-websockets", type=int)
    config.add_argument("--cpuset", help="dedicated API CPU list, e.g. 0-7")
    config.add_argument("--db-cpuset", help="disjoint PostgreSQL CPU list, e.g. 8-15")
    config.add_argument("--bind-address", help="default 127.0.0.1; use a private benchmark interface for a remote load generator")
    config.add_argument("--http-port", type=int)
    config.add_argument("--https-port", type=int)
    config.add_argument("--cgroup-parent", help="Docker cgroup parent, e.g. pgxbench-api.slice")
    config.add_argument("--cgroup-path", help="actual parent path on Linux Docker host")
    config.add_argument("--external-db-env", help="name of environment variable with restricted external benchmark DB URL")
    config.add_argument("--vm-memory-mib", type=int, help="actual provisioned VM RAM for whole-host qualification")
    up = sub.add_parser("up", help="start only the generated benchmark project")
    up.add_argument("--build", action="store_true")
    sub.add_parser("down", help="stop the benchmark project; preserve fixture volumes")
    seeds = sub.add_parser("seed", help="reset ONLY the benchmark fixture tables")
    seeds.add_argument("--rows", type=int, default=10000)
    seeds.add_argument("--tenants", type=int, default=100)
    seeds.add_argument("--payload-bytes", type=int, default=128)
    seeds.add_argument("--supabase", action="store_true", help="install private Broadcast policy/trigger after Realtime migration")
    sub.add_parser("lock", help="record exact running image IDs/digests")
    observation = sub.add_parser("stats", help="write operator-side PostgreSQL connections/waits/function counters")
    observation.add_argument("--output", type=Path, required=True)
    netem = sub.add_parser("network-delay", help="Linux host: delay DB traffic inside only benchmark API network namespaces")
    netem.add_argument("--delay-ms", type=float, required=True, help="outbound DB-packet delay; 0 clears")
    verify = sub.add_parser("verify", help="short functional checks; starts/stops/reseeds ONLY the isolated project")
    verify.add_argument("--targets", default="pgxio,node,go,openresty,postgrest,supabase")
    verify.add_argument("--url", default="http://localhost:18080")
    verify.add_argument("--build", action="store_true")
    verify.add_argument("--wrk-image", help="also verify wrk/wrk2 adapters (adds about 21 seconds per target)")
    verify.add_argument("--output", type=Path, default=REPO / "artifacts" / "bench-validation")
    tokens = sub.add_parser("token", help="print a disposable benchmark JWT (not a production token)")
    tokens.add_argument("--tenant", type=int, default=0)
    tokens.add_argument("--seconds", type=int, default=172800)
    export = sub.add_parser("export-client", help="export load-generator configuration and disposable signing key, no DB credentials")
    export.add_argument("--output", type=Path, required=True)
    load = sub.add_parser("run", help="run HTTP/realtime load; never starts or reconfigures servers")
    load.add_argument("--target", choices=TARGETS)
    load.add_argument("--engine", choices=("auto", "wrk", "wrk2", "python"), default="auto",
                      help="auto: wrk2 for REST, Python for realtime/correctness")
    load.add_argument("--purpose", choices=("measurement", "functional"), default="measurement")
    load.add_argument('--auth-mode', choices=('jwt', 'none'), help='must match the rendered server profile')
    load.add_argument('--auth-fixture', choices=('tenant', 'public'), help='must match the rendered server fixture')
    load.add_argument("--wrk-binary", help="path to native wrk/wrk2 executable")
    load.add_argument("--wrk-image", help="optional Docker image containing wrk/wrk2, e.g. pgxbench-wrk")
    load.add_argument("--wrk-cpuset", help="Docker load-generator CPU list, separate from API/database cores")
    load.add_argument("--threads", type=int, default=2, help="load-generator threads, independent of server CPUs")
    load.add_argument("--connections", type=int, default=16, help="HTTP connections for wrk/wrk2")
    load.add_argument("--url", default="http://localhost:18080")
    load.add_argument("--ws-url")
    load.add_argument("--ca", help="trusted benchmark TLS certificate / CA file")
    load.add_argument("--api-key-env", help="optional Supabase gateway API key environment variable")
    load.add_argument("--workload", choices=("one", "page", "write", "mixed", "small-app"), default="page")
    load.add_argument("--rate", type=float, default=50)
    load.add_argument("--duration", type=positive, default=300)
    load.add_argument("--warmup", type=float, default=120)
    load.add_argument("--sockets", type=int)
    load.add_argument("--subscriptions", type=int, default=1)
    load.add_argument("--active-tenants", type=int, default=10)
    load.add_argument("--realtime-mode", choices=("notify", "refetch", "both"), default="both")
    load.add_argument("--probe-rate", type=positive, default=1)
    load.add_argument("--max-inflight", type=int, default=256)
    load.add_argument("--timeout", type=positive, default=10)
    load.add_argument("--event-timeout", type=positive, default=5)
    load.add_argument("--seed", type=int, default=1)
    load.add_argument("--slow-reader-fraction", type=float, default=0)
    load.add_argument("--slow-reader-delay", type=positive, default=1)
    load.add_argument("--output", type=Path, required=True)
    resources = sub.add_parser("collect", help="collect cgroup v2/PSS on Linux Docker HOST, outside measured containers")
    resources.add_argument("--output", type=Path, required=True)
    resources.add_argument("--interval", type=positive, default=1)
    resources.add_argument("--duration", type=float, default=0, help="seconds; 0 means until Ctrl-C")
    limits = sub.add_parser("limits", help="apply aggregate limits to an existing dedicated Linux child cgroup")
    limits.add_argument("--cgroup", required=True)
    limits.add_argument("--cpus", type=positive, required=True)
    limits.add_argument("--memory-mib", type=int, default=0)
    limits.add_argument("--cpuset")
    jobs = sub.add_parser("matrix", help="write a randomized campaign specification; does not run it")
    jobs.add_argument("--kind", choices=("cpu", "ram", "host", "realtime", "soak"), required=True)
    jobs.add_argument("--targets", default="pgxio,postgrest,openresty,node,go,supabase")
    jobs.add_argument('--auth-modes', default='jwt', help='comma-separated jwt,none; none requires public REST fixtures')
    jobs.add_argument('--auth-fixture', choices=('tenant', 'public'), default='tenant')
    jobs.add_argument('--jwt-cache', choices=('default', 'off'), default='default')
    jobs.add_argument("--rates", type=comma_ints, default=[50, 100, 200, 400, 800, 1600])
    jobs.add_argument("--memory-values", type=comma_ints, default=[16, 32, 64, 128, 256, 512, 1024, 2048, 4096])
    jobs.add_argument("--repetitions", type=int)
    jobs.add_argument("--duration", type=positive)
    jobs.add_argument("--warmup", type=float, default=120)
    jobs.add_argument("--rows", type=int, default=10000)
    jobs.add_argument("--seed", type=int, default=20261015)
    jobs.add_argument("--output", type=Path, required=True)
    campaign = sub.add_parser("campaign", help="inspect or execute a saved matrix")
    campaign.add_argument("--file", type=Path, required=True)
    campaign.add_argument("--output", type=Path, default=REPO / "artifacts" / "bench-results")
    campaign.add_argument("--execute", action="store_true", help="actually configure/start/reseed isolated benchmark services")
    campaign.add_argument("--job", type=int, help="run one zero-based job index")
    campaign.add_argument("--collect", action="store_true", help="Linux host resource evidence")
    campaign.add_argument("--url", default="http://localhost:18080")
    campaign.add_argument("--startup-timeout", type=positive, default=120)
    campaign.add_argument("--stop-on-failure", action="store_true")
    campaign.add_argument("--wrk-image", help="run REST load using the supplied wrk/wrk2 image")
    auth = sub.add_parser('auth-compare', help='paired JWT/no-JWT REST experiments; dry-run unless --execute')
    auth.add_argument('--pgxio-image')
    auth.add_argument('--module-options', type=json.loads)
    auth.add_argument('--execute', action='store_true')
    auth.add_argument('--collector-host', choices=('none', 'local', 'colima'), default='none')
    auth.add_argument('--targets', default='pgxio,postgrest')
    auth.add_argument('--connections', type=comma_ints, default=[1, 8])
    auth.add_argument('--repetitions', type=int, default=3)
    auth.add_argument('--duration', type=positive, default=30)
    auth.add_argument('--warmup', type=float, default=10)
    auth.add_argument('--seed', type=int, default=20261015)
    auth.add_argument('--wrk-image', default='pgxbench-wrk')
    auth.add_argument('--wrk-cpuset')
    auth.add_argument('--api-cpuset')
    auth.add_argument('--db-cpuset')
    auth.add_argument('--url', default='http://localhost:18080')
    auth.add_argument('--output', type=Path, default=REPO / 'artifacts' / 'bench-auth-comparison')
    report = sub.add_parser("report", help="write Markdown/CSV/JSON summaries; consumes *.resources.jsonl when present")
    report.add_argument("--results", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--include-functional", action="store_true")
    qualify = sub.add_parser("qualify", help="check three one-hour trials and bracketed API RAM floor")
    qualify.add_argument("--results", type=Path, required=True)
    qualify.add_argument("--output", type=Path, required=True)
    exercise = sub.add_parser("exercise", help="functional resilience checks against the isolated stack")
    exercise.add_argument("--scenario", choices=("reload", "listener-loss", "database-restart", "revocation", "reconnect", "burst"), required=True)
    exercise.add_argument("--url", default="http://localhost:18080")
    exercise.add_argument("--sockets", type=int, default=10)
    exercise.add_argument("--cycles", type=int, default=10)
    exercise.add_argument("--burst-size", type=int, default=100)
    exercise.add_argument("--timeout", type=positive, default=60)
    exercise.add_argument("--output", type=Path, required=True)
    inputs = sub.add_parser('input-compare', help='before/raw/schema REST input trials; dry-run unless --execute')
    inputs.add_argument('--execute', action='store_true')
    inputs.add_argument('--baseline-image', default='pgxio:rest-input-before')
    inputs.add_argument('--image', default='pgxio:rest-input')
    inputs.add_argument('--body-sizes', type=comma_ints, default=[1024, 16384, 65536])
    inputs.add_argument('--connections', type=int, choices=(1,8,32), default=8)
    inputs.add_argument('--repetitions', type=int, default=3)
    inputs.add_argument('--duration', type=positive, default=30)
    inputs.add_argument('--warmup', type=float, default=10)
    inputs.add_argument('--seed', type=int, default=20261016)
    inputs.add_argument('--collector-host', choices=('none','local','colima'), default='none')
    inputs.add_argument('--api-cpuset')
    inputs.add_argument('--db-cpuset')
    inputs.add_argument('--wrk-cpuset')
    inputs.add_argument('--wrk-image', default='pgxbench-wrk')
    inputs.add_argument('--url', default='http://localhost:18080')
    inputs.add_argument('--output', type=Path, default=REPO/'artifacts/rest-input-comparison')
    validation = sub.add_parser('validation-compare', help='optional-contract and output-validation comparison')
    validation.add_argument('--baseline-image', default='pgxio:native-config-before')
    validation.add_argument('--image', default='pgxio:native-config')
    validation.add_argument('--execute', action='store_true')
    validation.add_argument('--collector-host', choices=('none','local','colima'), default='none')
    validation.add_argument('--connections', type=comma_ints, default=[8])
    validation.add_argument('--workloads', default='one,page')
    validation.add_argument('--repetitions', type=int, default=3)
    validation.add_argument('--duration', type=positive, default=20)
    validation.add_argument('--warmup', type=float, default=5)
    validation.add_argument('--seed', type=int, default=20261018)
    validation.add_argument('--wrk-image', default='pgxbench-wrk')
    validation.add_argument('--wrk-cpuset')
    validation.add_argument('--api-cpuset')
    validation.add_argument('--db-cpuset')
    validation.add_argument('--url', default='http://localhost:18080')
    validation.add_argument('--output', type=Path, default=REPO/'artifacts/validation-comparison')
    borrowed = sub.add_parser('borrowed-compare', help='matched libpq control/copy/borrowed trials; dry-run by default')
    borrowed.add_argument('--execute', action='store_true')
    borrowed.add_argument('--unpatched-image', default='pgxio:libpq-control')
    borrowed.add_argument('--image', default='pgxio:borrowed-libpq')
    borrowed.add_argument('--body-sizes', type=comma_ints, default=[1024,16384,65536])
    borrowed.add_argument('--connections', type=comma_ints, default=[8,32])
    borrowed.add_argument('--database-tls', default='disable,verify-full')
    borrowed.add_argument('--repetitions', type=int, default=3)
    borrowed.add_argument('--duration', type=positive, default=30)
    borrowed.add_argument('--warmup', type=float, default=10)
    borrowed.add_argument('--seed', type=int, default=20261016)
    borrowed.add_argument('--collector-host', choices=('none','local','colima'), default='none')
    borrowed.add_argument('--api-cpuset')
    borrowed.add_argument('--db-cpuset')
    borrowed.add_argument('--wrk-cpuset')
    borrowed.add_argument('--wrk-image', default='pgxbench-wrk')
    borrowed.add_argument('--url', default='http://localhost:18080')
    borrowed.add_argument('--output', type=Path, default=REPO/'artifacts/borrowed-comparison')
    db = sub.add_parser('db-baseline', help='separate direct SQL pgbench baseline (Colima host sampler)')
    db.add_argument('--connections', type=comma_ints, default=[1, 8, 32])
    db.add_argument('--repetitions', type=int, default=3)
    db.add_argument('--duration', type=int, default=30)
    db.add_argument('--warmup', type=int, default=10)
    db.add_argument('--workload', choices=('one', 'page', 'write', 'mixed'), default='page')
    db.add_argument('--image', default='pgxio:perf-auth')
    db.add_argument('--api-cpuset', default='0')
    db.add_argument('--db-cpuset', default='1')
    db.add_argument('--generator-cpuset', default='2-3')
    db.add_argument('--output', type=Path, required=True)
    perf = sub.add_parser('perf-compare', help='randomized immutable-image performance stages; dry-run by default')
    perf.add_argument('--execute', action='store_true')
    perf.add_argument('--collector-host', choices=('none', 'local', 'colima'), default='none')
    perf.add_argument('--database-tls', choices=('verify-full', 'disable'), default='verify-full')
    perf.add_argument('--baseline-image', default='pgxio:perf-baseline-auth')
    perf.add_argument('--image', default='pgxio:perf-auth')
    perf.add_argument('--profiles', default='baseline,recovery,prepared,pipeline,queue,cache,postgrest-uncached,postgrest-cached')
    perf.add_argument('--workloads', default='page')
    perf.add_argument('--connections', type=comma_ints, default=[1,8,32])
    perf.add_argument('--repetitions', type=int, default=3)
    perf.add_argument('--duration', type=positive, default=30)
    perf.add_argument('--warmup', type=float, default=10)
    perf.add_argument('--database-delay-ms', type=float, default=0, help='Linux Docker host only: outbound database packet delay; record actual RTT separately')
    perf.add_argument('--cpus', type=int, choices=(1,2,4,8), default=1)
    perf.add_argument('--workers', type=int, choices=(1,2,4,8))
    perf.add_argument('--nginx-profiles', default='baseline')
    perf.add_argument('--seed', type=int, default=20261015)
    perf.add_argument('--wrk-image', default='pgxbench-wrk')
    perf.add_argument('--api-cpuset')
    perf.add_argument('--db-cpuset')
    perf.add_argument('--wrk-cpuset')
    perf.add_argument('--url', default='http://localhost:18080')
    perf.add_argument('--output', type=Path, default=REPO / 'artifacts/perf-comparison')
    return p


def export_client(workdir, output):
    output = Path(output).resolve()
    if output == Path(workdir).resolve():
        raise ValueError("client export directory must differ from the server workdir")
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    (output / "runtime").mkdir(exist_ok=True)
    dump(output / "settings.json", public_settings(load_settings(workdir)), private=True)
    for name in ("tls-cert.pem", "jwks.json"):
        shutil.copyfile(Path(workdir) / "runtime" / name, output / "runtime" / name)
    shutil.copyfile(Path(workdir) / "jwt-private.pem", output / "jwt-private.pem")
    (output / "jwt-private.pem").chmod(0o600)
    source = Path(workdir) / "images.lock.json"
    if source.exists():
        shutil.copyfile(source, output / source.name)
    return {"client_directory": str(output), "contains": "public settings, CA/JWKS, disposable benchmark JWT signing key; no DB passwords"}


def validate_run(args, p):
    if args.sockets is None:
        args.sockets = 100 if args.workload == "small-app" else 0
    if args.engine == "auto":
        args.engine = "python" if args.sockets else "wrk2"
    if args.engine != "python" and args.sockets:
        p.error("wrk/wrk2 handle HTTP only; use --engine python for the realtime profile")
    settings = load_settings(args.workdir)
    if (args.rate < 0 or args.warmup < 0 or args.sockets < 0 or args.max_inflight < 1
        or not math.isfinite(args.rate) or not math.isfinite(args.warmup)
        or not 1 <= args.subscriptions <= 16 or not 1 <= args.active_tenants <= settings["tenants"]
        or not 0 <= args.slow_reader_fraction <= 1 or args.threads < 1 or args.connections < args.threads):
        p.error("invalid workload limits")
    if args.sockets and (args.sockets < args.active_tenants or args.sockets % args.active_tenants):
        p.error("sockets must divide evenly across active tenants, with at least one socket per tenant")


def main():
    p = parser()
    args = p.parse_args()
    try:
        if args.command in ('input-compare', 'borrowed-compare'):
            if args.command == 'input-compare':
                from .input_compare import jobs
            else:
                from .borrowed_compare import jobs
            from .auth_compare import compare
            result = asyncio.run(compare(args, jobs(args)))
            if args.execute and not result['passed']:
                raise RuntimeError('input comparison stopped; see outcomes.json')
        elif args.command == 'db-baseline':
            from .pgbench import baseline
            if args.repetitions < 1 or args.duration < 10 or args.warmup < 0:
                raise ValueError('pgbench requires repetitions >= 1, duration >= 10, warmup >= 0')
            result = asyncio.run(baseline(args))
        elif args.command == 'validation-compare':
            from .validation_compare import jobs
            from .auth_compare import compare
            result = asyncio.run(compare(args, jobs(args)))
            if args.execute and not result['passed']:
                print(json.dumps(result, indent=2))
                raise RuntimeError('validation comparison stopped; see outcomes.json')
        elif args.command == 'perf-compare':
            from .perf_compare import jobs
            from .auth_compare import compare
            result = asyncio.run(compare(args, jobs(args)))
            if args.execute and not result['passed']:
                print(json.dumps(result, indent=2))
                raise RuntimeError('performance comparison stopped; see outcomes.json')
        elif args.command == "prepare":
            result = public_settings(prepare(args.workdir))
        elif args.command == "certificate":
            from .setup import certificate
            result = certificate(args.workdir, args.hostname, args.database)
        elif args.command == "render":
            updates = {k: v for k, v in vars(args).items() if k not in ("command", "workdir")}
            if updates.get("cpus") is not None and updates.get("workers") is None:
                updates["workers"] = updates["cpus"]
            result = public_settings(render(args.workdir, updates))
        elif args.command == "up":
            flags = ["up", "--detach", "--remove-orphans"] + (["--build"] if args.build else [])
            compose(args.workdir, *flags)
            result = {"started": load_settings(args.workdir)["target"], "project": "pgxbench"}
        elif args.command == "down":
            compose(args.workdir, "down", "--remove-orphans")
            result = {"stopped": "pgxbench", "volumes": "preserved"}
        elif args.command == "seed":
            result = seed(args.workdir, args.rows, args.tenants, args.payload_bytes, args.supabase)
        elif args.command == "lock":
            result = lock(args.workdir)
        elif args.command == "stats":
            from .operator import stats
            result = stats(args.workdir, args.output)
        elif args.command == "network-delay":
            from .operator import network_delay
            result = network_delay(args.workdir, args.delay_ms)
        elif args.command == "verify":
            from .verify import verify
            result = asyncio.run(verify(args))
            print(json.dumps(result, indent=2))
            if not result["passed"]:
                raise SystemExit(1)
            return
        elif args.command == 'auth-compare':
            from .auth_compare import compare
            result = asyncio.run(compare(args))
            print(json.dumps(result, indent=2))
            if result.get('passed') is False:
                raise SystemExit(1)
            return
        elif args.command == "token":
            if args.tenant < 0:
                p.error("tenant must be nonnegative")
            print(token(args.workdir, args.tenant, args.seconds))
            return
        elif args.command == "export-client":
            result = export_client(args.workdir, args.output)
        elif args.command == "run":
            from .load import run
            validate_run(args, p)
            report = asyncio.run(run(args))
            result = {"completed": report["completed"], "output": str(args.output), "results": report["results"]}
            # Histograms stay in the artifact; don't print hundreds of buckets.
            for value in result["results"].values():
                if isinstance(value, dict):
                    value.pop("histogram", None)
        elif args.command == "collect":
            from .resources import collect
            if args.duration < 0:
                p.error("duration cannot be negative")
            result = asyncio.run(collect(args.workdir, args.output, args.interval, args.duration))
        elif args.command == "limits":
            from .resources import apply_limits
            result = apply_limits(args.cgroup, args.cpus, args.memory_mib, args.cpuset)
        elif args.command == "matrix":
            from .campaign import matrix
            if args.repetitions is not None and args.repetitions < 1:
                p.error("repetitions must be positive")
            result = matrix(args)
        elif args.command == "campaign":
            from .campaign import execute
            result = asyncio.run(execute(args))
        elif args.command == "report":
            from .report import make_report
            result = make_report(args.results, args.output, args.include_functional)
        elif args.command == "qualify":
            from .report import qualify
            result = qualify(args.results, args.output)
        else:
            from .exercise import exercise
            result = asyncio.run(exercise(args))
        print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        print("Interrupted; completed trial artifacts are preserved.", file=sys.stderr)
        raise SystemExit(130)
    except subprocess.CalledProcessError as error:
        # Generated Compose files contain credentials; never print their content.
        message = error.stderr or "external command failed"
        try:
            settings = load_settings(args.workdir)
            for name in ("admin_password", "web_password", "realtime_secret", "secret_key_base"):
                if settings.get(name):
                    message = message.replace(settings[name], "[redacted]")
        except OSError:
            pass
        print(message[-4000:], file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)

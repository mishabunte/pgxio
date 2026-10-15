# Competitive benchmarks

The [approved plan](PLAN.md) defines what to measure. This directory supplies an isolated test stack, reference backends, wrk/wrk2 workloads, a realtime/correctness driver, Linux resource collection, campaign generation and reports. It does not contain competitive results.

All commands below run from the repository root. Python 3.11+ linked to **OpenSSL 1.1.1+ with TLS 1.3**, OpenSSL CLI and Docker Compose are required. macOS's Xcode Python uses old LibreSSL; use a current Homebrew/pyenv Python there. Install into a separate environment so the development environment is preserved:

```sh
python3.12 -m venv .venv-bench
.venv-bench/bin/python -m pip install -r benchmarks/requirements.txt
```

The default work directory is `artifacts/bench`, the Compose project is `pgxbench`, and the ports are **18080/18443**. The development project on 8080 is separate. `down` preserves benchmark volumes. `seed` resets only `pgxbench.rows` and `pgxbench.accounts` in the dedicated benchmark database.

## Transport profiles

**Direct HTTP/WS is the primary backend baseline**, modeling TLS termination at a separate front proxy. Use `render --frontend http` and the default `http://localhost:18080` URL. This disables frontend TLS listeners; standalone PostgREST exposes its HTTP port directly with no extra Nginx proxy. Its readiness endpoint uses loopback port 18081. Supabase retains its HTTP gateway to route REST and realtime. PostgreSQL TLS settings are independent and remain matched across targets.

Use `render --frontend tls` plus `run --url https://localhost:18443` for a separately labeled TLS-enabled experiment. This enables native TLS in custom adapters and the required TLS proxy for PostgREST/Supabase. It does not model a uniform external edge proxy across all competitors. Do not pool its results or RAM floors with direct HTTP. New work directories default to HTTP; existing saved experiments retain their original topology until explicitly rendered with `--frontend http`.

## First run

```sh
.venv-bench/bin/python benchmarks/bench.py prepare
.venv-bench/bin/python benchmarks/bench.py render --target pgxio --frontend http --cpus 1 --pool-mode growing
.venv-bench/bin/python benchmarks/bench.py up --build
.venv-bench/bin/python benchmarks/bench.py seed
.venv-bench/bin/python benchmarks/bench.py lock

# Short functional check, not a capacity measurement:
.venv-bench/bin/python benchmarks/bench.py run --engine python --purpose functional --rate 10 --duration 3 --warmup 0 \
  --output artifacts/bench-results/functional.json
```

Every run checks real responses, tenant isolation, an acknowledged versioned update, rejection of invalid/expired JWTs, and, for custom adapters, invalid-input rejection. Output validation happens before commit in pgxio and the custom references. Fixtures use a restricted `web` role with no private-table grants and explicit SECURITY DEFINER functions. Signing keys are disposable benchmark keys, never production keys.

An optional adapter verification also checks rollback of invalid output. It starts/reseeds/stops only the isolated project, restores its configuration, and leaves it stopped:

```sh
.venv-bench/bin/python benchmarks/bench.py verify --build
```

Supported targets: `pgxio`, `postgrest`, `openresty`, `node`, `go`, `supabase`. To switch targets:

```sh
.venv-bench/bin/python benchmarks/bench.py down
.venv-bench/bin/python benchmarks/bench.py render --target node --frontend http --cpus 1 --pool-mode growing
.venv-bench/bin/python benchmarks/bench.py up --build
.venv-bench/bin/python benchmarks/bench.py seed
.venv-bench/bin/python benchmarks/bench.py lock
```

Node and Go dependencies have committed lockfiles. Image release pins live in `pgxbench/setup.py`; `prepare` copies them into private `settings.json`. Review that file to change a pin for a new campaign. `lock` records immutable running image IDs/digests and architecture. Keep the same pins across trials.

## REST: wrk and wrk2

Use **wrk2 for constant-rate latency/capacity tests**, **wrk for maximum-throughput/concurrency tests**. Their results are labeled separately. `--engine auto` selects wrk2 for REST and Python when sockets are enabled.

Local ARM64 trials on 2026-10-15 exposed intermittent `hdr_histogram.c` assertions in the pinned wrk2 build with two threads/16 connections. Single-thread/single-connection functional checks passed, but they did not establish stability for larger runs. Until this is resolved or independently verified on the benchmark host, use `--engine python` for fixed-rate trials and retain its scheduler-delay/missed-arrival metrics. A wrk2 process crash is a generator failure, not zero backend capacity; private diagnostics are saved next to the trial. Ordinary wrk remains a separate closed-loop experiment.

Install native binaries on the load generator, or build the supplied image (both architectures use system LuaJIT/OpenSSL):

```sh
docker build -t pgxbench-wrk benchmarks/wrk

# Fixed rate: Lua pre-generates authenticated POST requests for all active tenants.
.venv-bench/bin/python benchmarks/bench.py run --engine wrk2 --wrk-image pgxbench-wrk \
  --workload page --rate 400 --threads 2 --connections 16 --duration 300 --warmup 120 \
  --output artifacts/bench-results/pgxio-fixed-rate.json

# Closed-loop maximum throughput. --rate does not pace ordinary wrk.
.venv-bench/bin/python benchmarks/bench.py run --engine wrk --wrk-image pgxbench-wrk \
  --workload mixed --threads 2 --connections 64 --duration 300 --warmup 120 \
  --output artifacts/bench-results/pgxio-max-throughput.json
```

Without `--wrk-image`, the runner uses `wrk`/`wrk2` from PATH. `--wrk-binary /absolute/path` selects another binary. The Docker wrapper uses the load generator's host network; it belongs on the **separate Linux load-generator host** for publication. Local Colima checks are functional evidence only.

The supplied Docker builds patch both generators’ request and event-loop timers to `CLOCK_MONOTONIC` and record that clock in result metadata. This avoids false timeouts and negative histogram indexes when a VM adjusts wall time. Rebuild the image after updating these tools; native binaries have an unknown clock source. Earlier local wall-clock runs are invalid for performance conclusions.

`--wrk-cpuset 2-3` pins a Docker load generator to dedicated cores. Use disjoint API/database/generator CPU sets when they share a test host.

Workloads are `one` (indexed row), `page` (100 rows), `write` (version increment) and `mixed` (90% pages/10% updates). Repeat concurrency counts 1, 4, 16, 64, 256 with threads dividing connections; use one thread for one connection. Keep load-generator threads independent of server CPUs. wrk2 requires integer RPS at least as large as its connection count, and at least 20 seconds per trial/warmup because calibration takes about ten seconds.

The runner retains raw wrk output, exact HTTP status counts, socket errors, native latency percentiles, and successful RPS. Non-200s never count as successful throughput. wrk's native histogram is a sample of completed responses; wrk2 reports schedule-corrected latency. Neither histogram represents latency for requests which never completed; socket errors and offered/completed gaps remain visible. Response bodies are validated before/after wrk runs; during load the Lua hook counts statuses without parsing JSON.

Native wrk does not verify TLS peer certificates. The control-plane preflight verifies the supplied CA first. Only disposable benchmark endpoints should be tested. The optional TLS profile uses TLS 1.3; the supplied build links modern system OpenSSL.

For a transport-only Nginx overhead baseline, stop the API stack and run the supplied configuration. This tiny static response has no JWT/SQL work and belongs in a separate overhead chart:

```sh
.venv-bench/bin/python benchmarks/bench.py down
docker run --rm --name pgxbench-baseline --cpus 1 --memory 64m --memory-swap 64m \
  -p 127.0.0.1:18443:8443 \
  -v "$PWD/benchmarks/nginx-baseline.conf:/etc/nginx/nginx.conf:ro" \
  -v "$PWD/artifacts/bench/runtime/tls-cert.pem:/bench/tls-cert.pem:ro" \
  -v "$PWD/artifacts/bench/runtime/tls-key.pem:/bench/tls-key.pem:ro" nginx:1.28.0-bookworm
# In another terminal, using a native load-generator binary:
wrk2 -t2 -c16 -d300s -R400 --latency https://localhost:18443/baseline
```

## Paired JWT and no-JWT REST tests

`--auth-mode jwt|none` selects request authentication; `--auth-fixture tenant|public` selects the SQL fixture. The default remains tenant-protected JWT access. The paired experiment uses **public fixtures in both modes**, with identical tenant inputs, SQL, response shapes, validation, transactions and query-pool limits. It measures authentication cost, not tenant authorization. Existing protected fixtures remain available for correctness tests and normal benchmarks.

No-JWT access currently supports pgxio and standalone PostgREST REST tests. It is not an anonymous realtime protocol. pgxio requires the separate `pgxio:bench-auth` image, built with Cargo feature `benchmark-auth`, plus explicit `benchmark_no_jwt` configuration. Only the three `bench_public_*` fixture endpoints qualify, with matching `web.bench_public_*` SQL functions. Ordinary endpoints still require JWT. Standard builds reject the benchmark configuration field, including when it is false.

The no-JWT image still initializes the JWT verifier for protected endpoints. These tests remove per-request authentication from public fixtures; they do not measure a binary stripped of authentication code or key memory. Both paired pgxio profiles use the same special image. PostgREST uses its restricted `web` anonymous role only for the isolated no-JWT fixture; that is not a production configuration recommendation.

The dedicated runner generates randomized jobs, builds the pgxio benchmark image, performs correctness/authentication checks, records image locks and before/after database statistics, and stops/restores the isolated stack when done:

```sh
# Inspect first: no containers or fixtures changed.
.venv-bench/bin/python benchmarks/bench.py auth-compare

# Five profiles: pgxio JWT/none; PostgREST JWT-uncached/none/JWT-default-cache.
# Defaults: 1/8 connections, three repetitions, 10s warmup + 30s measured.
# Thirty trials mean 20 minutes of load, plus deployment/fixture overhead.
.venv-bench/bin/python benchmarks/bench.py auth-compare --execute \
  --api-cpuset 0 --db-cpuset 1 --wrk-cpuset 2-3 \
  --output artifacts/bench-auth-comparison
```

Use `--duration 1 --warmup 0 --repetitions 1 --connections 1` for functional verification; these short runs are labeled functional. Use a fresh output directory for every execution. Successful execution means trials completed, not that every case passed latency/error SLOs. Compare the retained error counts too.

Postflight can wait up to the configured timeout for database pool recovery after generator sockets close. Recovery retries are recorded and occur outside the measurement; every timed HTTP 503 remains an error. Authentication failures are never retried.

For manual paired runs, render `--target pgxio --frontend http --auth-fixture public --auth-mode jwt --jwt-cache off`, start with `up --build`, seed and lock, then run ordinary wrk. Repeat with `--auth-mode none` after stopping/recreating the stack. Use the same configuration for PostgREST. `--jwt-cache off` disables PostgREST's verification cache; `default` explicitly sets its pinned-version default of 1,000 entries. Prepared-statement behavior is held unchanged. Reports and charts keep authentication mode, fixture, cache configuration and concurrency separate.

### Configuration differences to keep visible

The pinned PostgREST 14 defaults include a 10-connection query pool, a 10-second pool-acquisition wait, 1,000 cached JWTs, prepared statements enabled, and error-level logging ([configuration reference](https://docs.postgrest.org/en/v14/references/configuration.html)). This harness sets eight query connections, a one-second acquisition wait, critical-only logging, and explicit CPU parallelism. JWT caching is a separate profile; prepared statements remain enabled.

The pgxio baseline uses one Nginx worker per selected worker count, access logging off, 30-second keep-alive, and at least 4,096 connections per worker. Nginx's unmodified keepalive request limit is 1,000 ([reference](https://nginx.org/en/docs/http/ngx_http_core_module.html#keepalive_requests)). The preserved pgxio baseline verifies JWTs per request, sends parameterized queries without a persistent prepared-statement cache, and returns HTTP 503 immediately when its query pool is busy. The new defaults enable preparation/pipelining and bounded acquisition waiting; token caching remains opt-in. Those API/database behaviors are not Nginx connection-limit settings. Record pool waiting/rejection and native query preparation when comparing overload behavior. See the Nginx tuning experiments in PLAN.md before changing the baseline.

## Realtime and the minimum-RAM workload

```sh
.venv-bench/bin/python benchmarks/bench.py run --workload small-app --duration 3600 --warmup 120 \
  --output artifacts/bench-results/pgxio-64m-r1.json

.venv-bench/bin/python benchmarks/bench.py run --engine python --workload page --rate 100 \
  --sockets 1000 --active-tenants 10 --subscriptions 16 --realtime-mode both \
  --duration 300 --warmup 120 --output artifacts/bench-results/pgxio-realtime.json
```

`small-app` creates 100 sockets across ten tenants, one subscription/socket and one rotating write/second. At the default total rate of 50, it schedules 39 foreground page reads, one probe write and about ten resulting snapshot refetches/second. All HTTP work is counted. Outside `small-app`, `--rate` is background HTTP RPS; probe writes and refetch traffic are additional and counted separately.

After a successful `small-app` trial, the driver reconnects/resubscribes all clients and verifies a fresh snapshot plus a versioned change. This recovery check stays outside steady-state latency/CPU metrics, and RAM qualification requires it to pass.

`--realtime-mode notify` isolates delivery; `refetch` or `both` also measures verified fresh data. Sockets subscribe before their initial snapshot. Snapshot refetches coalesce per socket. Monotonic versions distinguish stale data from receipt of an invalidation; bursts need not produce one event per write. Mixed background writes are disallowed during causal notification probes; use read background traffic and the separate burst exercise.

Use 0/100/1,000/10,000 sockets, 1/16 subscriptions, and vary `--active-tenants`: one tenant measures full fan-out; ten tenants affect one tenth of subscribers per write. Socket counts must divide evenly among active tenants. Raise `render --max-websockets` and the host descriptor limit before capacity runs. `--slow-reader-fraction 0.1 --slow-reader-delay 5` exercises slow readers. The bounded Python scheduler counts missed arrivals and refuses unbounded client-side queues. Confirm the generator itself has spare CPU before trusting a saturation point.

## Linux memory/CPU evidence

Run the collector on the **Linux Docker host**, outside measured containers. It discovers only the `pgxbench` project and reads host `/proc`, process PSS and cgroup v2 files directly. Root or equivalent permission to read all target `smaps_rollup` files is usually needed. It deliberately fails on macOS rather than reporting Docker Desktop/Colima VM numbers as host measurements.

On the server, start the collector before the load-generator command, using the **same result stem**:

```sh
sudo .venv-bench/bin/python benchmarks/bench.py collect \
  --duration 450 --output artifacts/bench-results/pgxio-fixed-rate.resources.jsonl
```

Copy that JSONL next to the corresponding run JSON before reporting. Synchronize clocks between server and generator; timestamp/configuration mismatches or missing samples invalidate resource evidence. The collector records process PSS/RSS, cgroup current/peak memory, anonymous/file/socket breakdown, CPU/throttling counters, OOM events, PSI, actual limits, container identity and host RAM/swap. Sampling does not spawn processes inside the measured cgroup.

For single-container targets, `render --memory-mib 64` applies a hard service limit with no swap. For **PostgREST in TLS mode + its proxy** and **Supabase's multiple services**, use a shared parent cgroup so every API component consumes the same total budget. Do not give each component the entire advertised limit.

For a systemd Docker host, a persistent anchor keeps the parent available before startup (the anchor's tiny footprint is part of the parent measurement):

```sh
sudo systemd-run --unit=pgxbench-anchor --slice=pgxbench-api.slice \
  --property=CPUAccounting=yes --property=MemoryAccounting=yes /usr/bin/sleep infinity
# systemd nests hyphenated slice names; confirm this path with:
systemctl show --property=ControlGroup --value pgxbench-api.slice

.venv-bench/bin/python benchmarks/bench.py render --target postgrest --frontend tls --cpus 1 \
  --cgroup-parent pgxbench-api.slice --cgroup-path /sys/fs/cgroup/pgxbench.slice/pgxbench-api.slice \
  --cpuset 0 --db-cpuset 1-4 --memory-mib 128
sudo .venv-bench/bin/python benchmarks/bench.py limits \
  --cgroup /sys/fs/cgroup/pgxbench.slice/pgxbench-api.slice --cpus 1 --cpuset 0 --memory-mib 128
.venv-bench/bin/python benchmarks/bench.py up
```

On a cgroupfs Docker host, supply its actual dedicated parent/path instead. The tools never change the root cgroup. Set the shared limit **before** startup. Reserve disjoint API/database cores and separate generator resources; CPU quotas alone do not provide exclusive cores. Keep memory ample while changing CPU count. The fixed pool is eight total query connections; the growing pool is four/worker. Go uses one process and `GOMAXPROCS`, with an equivalent total query pool; Node/Nginx/OpenResty use one worker/allocated CPU. Listener counts are native architecture costs, not query-pool capacity.

For whole-VPS tests, boot actual VMs at the plan's RAM sizes, disable swap, run the same profile and collect host evidence. API container limits do not simulate Linux/Docker startup or establish a VPS minimum. Use an external DB for API-only VPS trials; retain local PostgreSQL for complete-stack trials. Record actual provisioned RAM with `render --vm-memory-mib`. PostgreSQL uses a documented 32 MiB shared-buffer benchmark profile; DB configuration is part of every result.

Capture PostgreSQL connections, wait events, function call/time counters and database statistics before/after a timed trial:

```sh
.venv-bench/bin/python benchmarks/bench.py stats --output artifacts/bench-results/db-before.json
# Run load on the generator.
.venv-bench/bin/python benchmarks/bench.py stats --output artifacts/bench-results/db-after.json
```

Do not poll these through processes inside the API container. Function tracking is enabled equally in the generated local database configurations. External PostgreSQL hosts require their own CPU/memory monitoring.

For the remote-database latency comparison, `network-delay` uses host `nsenter`/`tc` (Linux operator privileges, iproute2/util-linux required). It changes only the dedicated benchmark API namespaces, filtering by the configured PostgreSQL destination/port:

```sh
sudo .venv-bench/bin/python benchmarks/bench.py network-delay --delay-ms 10
# Start collector and load; record measured DB RTT rather than assuming it.
sudo .venv-bench/bin/python benchmarks/bench.py network-delay --delay-ms 0
```

It delays outbound DB packets, not frontend responses. Reapply after recreating containers, and clear it before unrelated trials.

## Separate database and load generator

An external benchmark database must be dedicated, named `pgxbench`, and provisioned from `runtime/init.sql` using its operator credentials. It is not a production database. Use `PGXBENCH_ADMIN_URL` only on the operator/fixture host for seeding. The API URL must use the restricted `web` role, a numeric `hostaddr` for pgxio, and appropriate TLS verification. Reference drivers receive the same URL without libpq's `hostaddr` parameter.

```sh
# Supply these through your secret manager, not committed files or public reports.
# PGXBENCH_WEB_URL: restricted web URL, database pgxbench, hostaddr, sslmode=verify-full
# PGXBENCH_ADMIN_URL: operator URL for that disposable database
.venv-bench/bin/python benchmarks/bench.py render --target pgxio --external-db-env PGXBENCH_WEB_URL
.venv-bench/bin/python benchmarks/bench.py seed
```

For a private benchmark interface and a remote generator:

```sh
.venv-bench/bin/python benchmarks/bench.py certificate --hostname bench.internal.example
.venv-bench/bin/python benchmarks/bench.py render --frontend tls --bind-address 0.0.0.0
.venv-bench/bin/python benchmarks/bench.py up
.venv-bench/bin/python benchmarks/bench.py export-client --output artifacts/bench-client
```

Restrict the interface to the benchmark network. Copy `benchmarks/` and the client export to the generator, install requirements and build/install wrk there, then use:

```sh
.venv-bench/bin/python benchmarks/bench.py --workdir /path/to/bench-client run \
  --url https://bench.internal.example:18443 --engine wrk2 --rate 400 \
  --output /path/to/results/trial.json
```

The export contains a **disposable JWT signing key**, CA/JWKS and public experiment settings, not database passwords. Regenerate the export after changing the target, resources, fixture size, certificate or image lock. Do not copy the full server work directory to the generator. Optional private PostgreSQL CAs must be mounted inside `/bench` and referenced by their container path in the connection URL.

## Supabase-specific setup and feature gaps

The `supabase` target runs **PostgREST, a TLS proxy and Supabase Realtime**, with the official Supabase PostgreSQL image supplying its required schemas/roles/extensions. Studio/Auth/Storage/analytics are not included. Its database image/version differs from the plain PostgreSQL baseline and is recorded; this is a separately labeled product comparison, not an identical database implementation.

```sh
.venv-bench/bin/python benchmarks/bench.py down
.venv-bench/bin/python benchmarks/bench.py render --target supabase --frontend http
.venv-bench/bin/python benchmarks/bench.py up
# Wait for Realtime's tenant migrations before installing Broadcast policy/trigger:
.venv-bench/bin/python benchmarks/bench.py seed --supabase
.venv-bench/bin/python benchmarks/bench.py lock
.venv-bench/bin/python benchmarks/bench.py run --workload small-app --duration 3600 \
  --output artifacts/bench-results/supabase-small-app.json
```

Private Broadcast channels carry minimal change messages; the runner uses the Phoenix protocol and fetches snapshots through PostgREST. The native adapter supports **one private topic/socket**. Sixteen-subscription parity is explicitly unsupported. Channel authorization is cached natively, so revocation behavior is reported separately. PostgREST has no manifest input/output JSON Schema layer; ready-made product results retain that difference. OpenResty's validator implements the complete fixture schema subset, not arbitrary JSON Schema. Custom Node/Go/OpenResty JWT verification is limited to the benchmark's RS256 keys and is not a production auth library.

## Campaigns, resilience and reports

Matrix generation is non-executing. It reports the measured runtime before you choose to execute:

```sh
.venv-bench/bin/python benchmarks/bench.py matrix --kind cpu --targets pgxio,postgrest,openresty,node,go \
  --rows 1000000 --output artifacts/bench-cpu.json
.venv-bench/bin/python benchmarks/bench.py campaign --file artifacts/bench-cpu.json

# Local orchestration; use a separate generator for published results.
.venv-bench/bin/python benchmarks/bench.py campaign --file artifacts/bench-cpu.json \
  --execute --collect --wrk-image pgxbench-wrk --output artifacts/bench-results/cpu

.venv-bench/bin/python benchmarks/bench.py matrix --kind ram --targets pgxio,node,go,openresty,supabase \
  --memory-values 16,32,64,128,256,512,1024,2048,4096 --output artifacts/bench-ram.json
# Refine the first passing bracket with --memory-values 48,64 (for example).

# Whole-host specifications: resize/boot the VM yourself, then select the matching --job.
.venv-bench/bin/python benchmarks/bench.py matrix --kind host --targets pgxio \
  --memory-values 128,256,384,512,768,1024,2048,4096,8192 --output artifacts/bench-host.json
```

CPU matrices randomize 1/2/4/8 CPUs, both pool modes, all four REST workloads, offered rates and repetitions. Realtime matrices vary socket/subscription counts. RAM defaults are three one-hour trials; throughput defaults are five five-minute trials with two-minute warmups. `--kind soak --repetitions 1 --memory-values 128` generates a 24-hour confirmation at a chosen allocation. Campaign execution reconfigures/reseeds only its isolated project. Prebuild images before measuring startup; build time and cached readiness are labeled separately. Use `--job N` to select a saved job and a fresh output directory for a rerun.

Functional fault exercises run separately from steady-state SLO trials:

```sh
.venv-bench/bin/python benchmarks/bench.py exercise --scenario reload --output artifacts/bench-results/reload.json
.venv-bench/bin/python benchmarks/bench.py exercise --scenario listener-loss --output artifacts/bench-results/listener-loss.json
.venv-bench/bin/python benchmarks/bench.py exercise --scenario database-restart --output artifacts/bench-results/db-restart.json
.venv-bench/bin/python benchmarks/bench.py exercise --scenario revocation --output artifacts/bench-results/revocation.json
.venv-bench/bin/python benchmarks/bench.py exercise --scenario reconnect --sockets 100 --cycles 10 --output artifacts/bench-results/reconnect.json
.venv-bench/bin/python benchmarks/bench.py exercise --scenario burst --burst-size 100 --output artifacts/bench-results/burst.json

.venv-bench/bin/python benchmarks/bench.py report --results artifacts/bench-results --output artifacts/bench-report
.venv-bench/bin/python benchmarks/bench.py qualify --results artifacts/bench-results --output artifacts/bench-report/ram.json
.venv-bench/bin/python benchmarks/bench.py down
```

Reports produce Markdown/CSV/JSON and a standalone interactive `charts.html` for CPU scaling, latency and memory/socket plots. Raw resource traces also become time-series CSVs under `resource-samples/` for soak and retention analysis. Functional verification artifacts are excluded by default (`report --include-functional` is available for inspecting tools). Per-trial percentiles are preserved, not averaged. Qualification is conservative: load-only results cannot establish RAM, and an untested lower boundary remains “lowest observed pass,” not “minimum.” A recommended allocation still needs startup/reload checks and a 24-hour soak. Whole-VM qualification checks observed host RAM against the declared VM size; it never adds process PSS to infer VM RAM. REST-only RAM results (including standalone PostgREST) are labeled separately from the combined REST/realtime workload.

Run tool tests with `.venv-bench/bin/python -m unittest discover -s benchmarks/tests -v`. These are control-plane checks, not the actual competitive benchmark campaign.

## Runtime performance stages and Nginx experiments

Preserve the pre-change benchmark-feature image before rebuilding. `perf-compare` uses existing images and never rebuilds/retags them. All stage options, image locks, CPU settings and Nginx profiles stay in each trial configuration; reports do not merge different configurations.

```sh
# Before changing runtime code:
docker tag pgxio:bench-auth pgxio:perf-baseline-auth
# After implementing a stage:
docker build --build-arg CARGO_FEATURES=benchmark-auth -t pgxio:perf-auth .
# Inspect the randomized specification (no containers changed):
.venv-bench/bin/python benchmarks/bench.py perf-compare
# A representative execution; use a fresh output directory:
.venv-bench/bin/python benchmarks/bench.py perf-compare --execute \
  --profiles baseline,recovery,prepared,pipeline,queue,cache,postgrest-uncached,postgrest-cached \
  --workloads one,page,write,mixed --connections 1,8,32 \
  --api-cpuset 0 --db-cpuset 1 --wrk-cpuset 2-3 \
  --output artifacts/perf-comparison
```

The complete example is long: eight profiles × four workloads × three concurrency levels × three repetitions = 288 trials, approximately 192 minutes of load at the default 10-second warmup/30-second measurement, plus fixture/startup time. Select fewer profiles/workloads for the first comparison. Defaults retain only the page workload. Durations below ten seconds are functional checks.

Stages use the public-data JWT fixture: `baseline` sends no new config fields to the old image; `recovery` uses sequential unprepared execution with immediate pool rejection and predecoded keys; `prepared` adds preparation; `pipeline` adds batching; `queue` enables the one-second acquisition wait; `cache` adds a 1,000-entry/1-MiB token cache. Recovery and key predecoding share the first stage. PostgREST is tested with verification caching off and at its default capacity. Both products retain their native SQL behavior.

Nginx experiments are separate: `--nginx-profiles baseline,keepalive-10000,reuseport`. Use `--cpus 1|2|4|8` and an optional explicit `--workers` while retaining a fixed total pool of eight. Test `reuseport` with multiple workers and sufficient independent connections. On the Linux Docker host, add `--database-delay-ms 10` for the existing DB-destination netem experiment; the runner reapplies it after each container recreation. This needs the operator permissions/tools described under network-delay; it fails before deployment on macOS. Record measured RTT separately from the configured packet delay. These profiles do not change production defaults; use dedicated CPU assignments and enough server/generator capacity for scaling tests.

For a manual profile, `render --pgxio-image pgxio:perf-auth --module-options '{"db_pipeline":false,"db_prepared_statements":true}'` selects an existing image and exact module overrides. `--nginx-profile` selects one Nginx experiment. Omit new module fields when running a legacy image. Rendering does not start services.

Isolated regressions use a separate project/database/network and ports 18090/18490, and clean up their own containers:

```sh
docker build -t pgxio:perf .
.venv-bench/bin/python -m pip install './sdk/python[django]'
.venv-bench/bin/python tests/run-isolated.py --image pgxio:perf --suite all
.venv-bench/bin/python tests/run-isolated.py --image pgxio:perf --suite all --production --profile cache \
  --output artifacts/perf-validation/production-cache
```

Compatibility profiles are `sequential`, `unprepared`, and `failfast`. The performance suite uses one query connection so stable backend identity proves reuse after failures. The full existing integration suite uses four query connections. Production mode tests frontend HTTPS/WSS, RS256 JWKS and verify-full database TLS, including cancellation and key reload. Run one isolated suite at a time.

# pgxio competitive benchmark plan

Approved 2026-10-15. This is the test plan; benchmark results must be collected separately.

## Goal and competitors

Establish **minimum usable RAM, recommended deployment RAM, memory growth, and performance on 1/2/4/8 vCPUs**. Cover REST and realtime, with both external PostgreSQL and PostgreSQL on the same VPS.

Existing development-machine smoke results do not establish minimum RAM or comparative performance.

| Target | Comparison purpose | Workloads |
|---|---|---|
| PostgREST | Closest ready-made SQL-first REST competitor | Equivalent PostgreSQL RPC calls |
| OpenResty + pgmoon + lua-resty-websocket | Alternative using Nginx's event model | REST and equivalent authenticated invalidations |
| Node.js + Fastify + pg | Small conventional application backend | REST and equivalent invalidations |
| Go net/http + pgx | Small compiled application backend | REST and equivalent invalidations |
| PostgREST + Supabase Realtime | Ready-made REST/realtime alternative | RPC plus private database-triggered Broadcast |

PostgREST's LISTEN facility manages configuration/schema reloads, not client subscriptions. Its standalone realtime result is “unsupported.” Supabase Broadcast caches channel authorization; pgxio reauthorizes before invalidation. Publish that difference alongside resource use.

Keep bare Nginx as an overhead baseline. Exclude legacy c2h5oh and whole-platform Supabase from the primary ranking.

## Workloads and fairness

- Shared seeded PostgreSQL fixtures: 10,000 rows for minimum-RAM tests and 1 million rows for capacity tests. Include indexed single-row reads, 100-row lists, updates and a 90/10 read/write mix.
- Match SQL results, tenant permissions, transaction guarantees, JWT algorithm, response sizes and database connection budgets. Custom reference servers enforce fixture input/output contracts and reauthorize invalidations.
- Run a separate paired REST authentication-cost experiment with JWT and without JWT. Both profiles use identical public-data benchmark functions and explicit synthetic tenant inputs; they do not measure tenant authorization. Keep the existing tenant-protected workload separate. Disable PostgREST's JWT cache for matched per-request verification and record its cached-default profile separately. No-JWT pgxio access requires the benchmark-only build/configuration and cannot bypass ordinary endpoints; realtime continues requiring JWT.
- Run ready-made products with native behavior and document feature gaps rather than implying complete parity.
- Use direct HTTP/WS and RS256 for the primary backend comparison, modeling TLS termination at a separate front proxy. Standalone PostgREST must bypass the benchmark TLS proxy. Run TLS-enabled deployment experiments separately; record frontend topology and include its required proxy resources. Never combine transport profiles in a ranking or RAM floor.
- Benchmark notification delivery separately from write → notification → snapshot refetch → fresh client data.
- Minimum-load profile: **50 total HTTP requests/sec and 100 subscribed sockets**. Ten tenants have ten sockets each; 39 foreground reads/sec and one update/sec rotating among tenants cause approximately ten snapshot refetches/sec.

## Experiments

| Experiment | Configuration | Report |
|---|---|---|
| Minimum RAM | One vCPU; constrained memory; fresh start per candidate | Smallest allocation sustaining the defined workload |
| Memory footprint | Cold, warmed, sustained, after sockets close and during reload | API, database, proxy and whole-host memory |
| CPU scaling | 1/2/4/8 vCPUs with ample RAM | Successful RPS, latency, CPU/request, speedup and efficiency |
| REST saturation | Increasing arrival rates and a separate concurrency sweep | Maximum throughput within latency/error targets and overload behavior |
| Realtime capacity | 0/100/1,000/10,000 sockets; 1/16 subscriptions; increasing fan-out | Bytes/socket and subscription, notification/refetch latency and database work |
| Resilience/retention | Slow readers, bursts, reconnect storms, DB interruption, reload, repeated connection cycles | Peak memory, recovery time, correctness and retention |

Run realtime with HTTP idle and with concurrent HTTP traffic. Raise socket/file-descriptor limits explicitly; also report shipped defaults.

CPU scaling has two profiles: a fixed total query pool of eight and a growing pool of four connections per worker. pgxio uses `workers × (pool_size + 1)` database connections including listeners, with temporary reload overlap. Set each competitor's parallelism explicitly.

Keep database resources fixed and separate during API scaling. Measure DB CPU, connections, query latency and waits to identify saturation. Repeat representative runs with 10 ms additional database RTT.

## Nginx tuning experiments

Keep the shipped configuration as a baseline and record a distinct configuration for each tuning change. Current pgxio settings already disable access logging, enable 30-second HTTP keep-alive, and allow 4,096 connections per worker. One worker matches the current one-CPU comparison; extra workers on that CPU are an experiment, not an assumed improvement.

- On 1/2/4/8 allocated CPUs, compare explicit matching worker counts. Keep total query connections fixed at eight first, then use the separately labeled growing-pool profile. Include one listener per worker in database and memory accounting.
- At sustained keep-alive load, compare the pinned Nginx default keepalive request limit with explicit 1,000 and 10,000 limits. Measure connection churn and retained memory; increasing this limit can retain per-connection allocations longer.
- Test `reuseport` with multiple workers and enough independent connections. Persistent connections remain attached to a worker, so a one-connection test cannot demonstrate multiworker scaling. Compare worker affinity only with verified container CPU assignments.
- For high-concurrency/realtime cases, verify effective file-descriptor limits and increase `worker_connections` only when the measured workload approaches the limit. Count client and database sockets; record idle and peak memory.
- Use a separate connection-churn workload for listen backlog, accept behavior and keep-alive timeout experiments. The current 1/8 persistent-connection trials do not diagnose accept/backlog limits.
- Keep logging, frontend HTTP/TLS, response validation, fixtures and database limits matched. Record error rate, successful RPS, p95/p99, CPU per success and memory for every profile; reject apparent gains bought by extra errors or an increased database budget.

Do not change Nginx settings midway through the JWT/no-JWT campaign. Follow the initial results with targeted changes, one variable at a time. Nginx documentation: [worker settings](https://nginx.org/en/docs/ngx_core_module.html), [keepalive and listen settings](https://nginx.org/en/docs/http/ngx_http_core_module.html).

## RAM qualification and measurement

Publish three distinct recommendations per transport/topology: minimum API service allocation including required frontend services; minimum VPS RAM with external PostgreSQL; and minimum VPS RAM with local PostgreSQL. The VPS figures include Linux and Docker.

Search API allocations from 16 MiB to 4 GiB by doubling, then refine in 16 MiB steps. Search actual VM RAM from 128 MiB to 8 GiB and refine in 64 MiB steps. Explicitly report boundaries outside the range.

Disable swap. A candidate must pass three independent one-hour runs with:

- At least 99.9% successful HTTP requests, HTTP p95 below 100 ms and invalidation p99 below one second.
- No OOM, unexpected restart, incorrect result or unauthorized delivery.
- Successful reconnect/resubscribe followed by a current snapshot.

Report fresh-data latency including refetches. Measure startup/reload peaks separately. Choose recommended RAM above the floor with at least 25% headroom; verify it with a 24-hour soak.

Collect aggregate process PSS, RSS separately, cgroup current/peak memory, cache/socket breakdown, OOM events and pressure. Run collectors outside measured containers.

## Execution and deliverables

- Full matrix on dedicated x86-64 Linux resources; confirm pgxio RAM floor and representative CPU/realtime results on native ARM64.
- Separate load-generator host. Use open-arrival load and record offered load, successful throughput, errors, timeouts and unscheduled requests.
- Use wrk2 as the primary REST fixed-rate driver, wrk for closed-loop maximum-throughput/concurrency sweeps, and the Python driver for realtime, refetches and correctness. Keep these load models separate in reports. wrk2 needs at least 20-second trials because of its calibration period.
- Throughput trials: two minutes warmup, five minutes measured, five repetitions in randomized competitor order.
- Pin versions, image digests, configuration, fixtures and hardware. Publish result variation and every failure status.
- Preserve the existing smoke harness; add competitor adapters, sustained workloads and external resource collection. Keep fixtures separate from product examples; no public pgxio API change.
- Publish commands, raw JSON/CSV, a feature-equivalence table, RAM recommendations, throughput/latency curves, CPU scaling curves and realtime memory slopes.

“Multiple CPUs” means allocated vCPUs/cores, not multiple physical CPU sockets. Recommendations apply to the stated workload/dataset. Larger databases and connection counts get separate capacity results.

## Sources

- [PostgREST RPC](https://docs.postgrest.org/en/stable/references/api/functions.html) and [listener](https://docs.postgrest.org/en/stable/references/listener.html)
- [pgmoon](https://github.com/leafo/pgmoon) and [lua-resty-websocket](https://github.com/openresty/lua-resty-websocket)
- [Supabase Realtime authorization](https://supabase.com/docs/guides/realtime/authorization) and [architecture](https://supabase.com/docs/guides/realtime/architecture)
- [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)
- [Open versus closed load models](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/)

## Implemented runtime performance stages (2026-10-16)

The runtime now reuses recovered sessions after drained rollback, prepares manifest/settings statements per connection, and batches BEGIN/settings/function while keeping output validation before a separate COMMIT. Cancellation uses libpq 17's nonblocking API and bounded recovery. New defaults enable preparation and pipelining, wait up to one second for acquisition, and admit at most 32 waiting jobs per worker. Immediate rejection remains configurable. Verification keys are decoded at startup; token caching is opt-in, bounded by entries and accounted bytes, and never caches SQL authorization decisions.

Use `perf-compare` for immutable baseline/stage images and cached/uncached PostgREST, with one/eight/32 connections, selectable workloads, repetitions and CPU/worker counts. Nginx keep-alive/reuseport profiles remain separate experiments. Linux-host database-delay experiments are reapplied per trial. Reports group by exact configuration/image identity. See README.md for all commands and limits.

The first shared-host run completed 24 ten-second stage trials (one repetition per case); four intentional immediate-rejection overload cases failed the load SLO. Separate two-worker Nginx/write/mixed checks were functional only. These checks do not establish minimum RAM, CPU efficiency, or a production ranking. Longer repeated trials and dedicated Linux resource evidence remain required.

## Next experiment: balanced CPU saturation

The primary goal is near-100% API CPU for both pgxio and PostgREST, measured
separately with PostgreSQL headroom. Provision enough identical DB capacity for
both APIs, retain the matched workload, then sweep concurrency and API workers
to identify API-bound throughput. Verify database CPU/waits, connection-pool
waiting, and generator capacity rather than inferring API saturation from RPS.
Keep an equal-API-CPU comparison and report each product's saturation point.

As a separate capacity experiment, find a configuration for each product
where its API and PostgreSQL are both near CPU saturation. Sweep database CPU
allocations first to remove the observed database ceiling, then API workers/CPU
allocations and offered load/concurrency. Start with the same page fixture and
eight-query-connection pool; change pool size only as a separately labeled sweep.
Use disjoint exclusive API/DB CPUs and a separate load-generator host. Keep RAM
ample and collect API/DB CPU, throttling, memory, throughput, latency and errors
for every point. Define near saturation as sustained 90–100% of the assigned CPU
capacity over the measured interval, while retaining latency/error SLO results.
CPU percentages above one core must be normalized by allocated capacity.

The required API:DB CPU ratio may differ for pgxio and PostgREST. Publish those
ratios and total CPU budgets explicitly; individually balanced configurations
are capacity experiments, separate from equal-resource comparisons. Include the
direct pgbench baseline to identify database ceilings. Do not count generator
starvation, quota throttling, error-only CPU load, or unrelated VM processes as
useful saturation. If both cannot approach saturation under the matched workload,
report the limiting resource rather than changing the workload to force a result.

# Benchmarks

The [competitive benchmark plan](../benchmarks/PLAN.md) and [benchmark tools/runbook](../benchmarks/README.md)
cover wrk/wrk2 REST tests, realtime/refetch tests, competitor deployments, Linux PSS/cgroup
collection, CPU/RAM matrices and qualification. Those tools are separate from the development
smoke harness below. Their inclusion does not constitute measured competitor results.

The repository includes a repeatable smoke harness, not a published capacity comparison. Run `scripts/benchmark.py` on a quiet Linux VPS before making throughput or 512 MiB deployment claims. The raw local result is [results/local-smoke.json](results/local-smoke.json).

## Historical development-machine result

These measurements precede connection recovery, prepared statements, pipelining, and bounded admission. They are not a current-image RAM floor. Use the competitive harness to measure the new image under explicit limits.

ARM64 Linux containers on Colima/macOS, one Nginx worker, four query connections, one listener, local PostgreSQL 17.11. The client ran outside the Linux VM. The host was shared with other workloads. HTTP used development HMAC tokens without TLS. The SQL endpoint returns up to 100 task objects; this is a small real query, not an empty response.

| Measurement | Observed value |
|---|---:|
| Plain Nginx master + worker PSS | 4.31 MiB |
| pgxio master + worker PSS, warmed after requests | 32.58 MiB |
| Incremental warm PSS over plain Nginx | 28.10 MiB |
| PostgreSQL aggregate PSS | 40.98 MiB |
| API + PostgreSQL process PSS | 73.56 MiB |
| 100 authenticated subscriptions, incremental API PSS | 13.8 KiB/socket |
| 5,000 requests at concurrency 4 | 5,000 HTTP 200; no errors |
| Successful requests/sec in this smoke run | 550 |
| REST latency p50 / p95 / p99 | 5.86 / 15.88 / 25.06 ms |
| Fan-out to 100 subscribers, p50 / p99 | 92.50 / 150.81 ms |
| Cached API container restart → readiness, three trials | 478–751 ms |

Fan-out includes the originating HTTP write, commit, PostgreSQL notification, and an authorization transaction for each subscription. This cost is real; reducing authorization round trips is a future optimization that must preserve revocation checks. An earlier, shorter run on the same shared host produced materially different throughput. These numbers are instrumentation evidence, not reproducible service-level promises.

The first measurement exposed a retained 64 KiB handshake scratch allocation per WebSocket. Releasing that allocation reduced the observed 100-socket PSS slope from about 67.9 KiB to 13.8 KiB/socket in the subsequent smoke run. TLS buffers, kernel buffers, allocator behavior and subscriber activity can change that slope.

PSS proportionally allocates shared pages and is more useful than summed RSS for Nginx and PostgreSQL. The report also records RSS and cgroup memory; they measure different things and must not be mixed. Process PSS does not include the full host OS, Docker daemon, file cache, backups, monitoring, or database growth. The Docker image-reported size is also recorded; it is not a measurement of full VPS disk consumption. Cached restart time excludes image pull, compilation, database bootstrap and migrations.

## Reproduce

Start the example stack, then an otherwise identical Nginx binary with no module:

```sh
docker run -d --name pgxio-nginx-baseline \
  --ulimit nofile=8192:8192 --read-only \
  --tmpfs /tmp:uid=10001,gid=10001,mode=1770 \
  -p 127.0.0.1:8081:8080 \
  -v "$PWD/deploy/nginx-baseline.conf:/etc/nginx/baseline.conf:ro" \
  pgxio:dev -c /etc/nginx/baseline.conf -g 'daemon off;'

.venv/bin/python scripts/benchmark.py --requests 5000 --concurrency 4 \
  --websockets 100 --baseline pgxio-nginx-baseline --measure-startup \
  --output artifacts/local-smoke.json
```

The Nginx baseline is a **memory lower bound**, not an equivalent competitor: it does no JWT verification, SQL, JSON Schema validation or subscriptions. `--measure-startup` restarts only the named API container after requests/subscriptions finish. The harness records failures as well as successful throughput, counts all request latencies, and does not hide 503s.

## Comparison protocol

Before publishing, implement and run these workloads on both 512 MiB and 4 vCPU / 8 GiB targets, with exact versions and CPU limits recorded:

| Target | REST workload | Realtime workload |
|---|---|---|
| pgxio Rust module | Current manifest endpoints | Current invalidation protocol |
| Legacy C c2h5oh | Port the same SQL semantics to a supported Nginx build; do not deploy its old bundled TLS | No assumed parity |
| PostgREST | RPC functions with the same authorization, query, output and pool limits | Mark absent or add a separately accounted service |
| OpenResty | Lua handlers with equivalent JWT, parameterization, schema checks and SQL timeouts | lua-resty-websocket with shared listener and equivalent reauthorization |
| Minimal Node and Go | Small servers with the same SQL and permission semantics | Comparable ephemeral invalidation, not durable CDC |

Run warmup then several sustained trials at increasing concurrency, reporting errors and saturation, p50/p95/p99, CPU per successful request, allocations/GC where applicable, PSS and cgroup memory. Measure bare Nginx versus loaded module, and API-only versus API+PostgreSQL. Use both local and remote databases; network latency magnifies the current multi-step transaction cost. Compare equivalent TLS and authentication modes.

For realtime, measure 0/100/1,000/10,000 idle sockets where limits allow, per-socket memory slope, event-to-client latency at several fan-out sizes, auth query rate, slow-reader behavior, bursts, reconnect storms, and simultaneous HTTP load. Report the PostgreSQL connection count and transient reload overlap. Run long enough to observe memory retention and reconnection behavior.

For deployment, separately record compressed transfer bytes, unpacked runtime files, total installed dependencies, cold image pull, cached start to readiness, migration time and full deployment time. Do not compare one project's source checkout plus dependencies with another project's stripped executable.

This historical smoke report contains no competitor ranking. The competitive harness now includes separate local PostgREST comparisons and immutable-image performance stages; see the runbook for reproduction. Shared-host trials do not establish a production ranking or RAM floor.

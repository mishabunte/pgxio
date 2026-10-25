# Benchmarks

The [benchmark tools and runbook](../benchmarks/README.md) provide isolated
PostgreSQL fixtures, PostgREST and other reference backends, wrk workloads, CPU and
memory collection, and reports. Publishing these tools does not establish a
capacity or competitor ranking for the current source snapshot.

## Historical pgxio / PostgREST comparison

Earlier ARM64 Linux/Colima campaigns used one API core, four PostgreSQL cores,
pool eight, 32 HTTP connections, the same PostgreSQL 17.11 SQL/data, and RS256
JWTs. PostgREST was version 14.17. pgxio used a frozen experimental-libpq image
with borrowing disabled and output validation enabled, not the current source
preview or its stock-libpq build. Each value is a median of three randomized
trials, each with 10 seconds warmup and 30 seconds measurement.

| RPC / input | JWT cache | DB TLS | pgxio RPS | PostgREST RPS | Throughput ratio | API PSS MiB: pgxio / PostgREST |
|---|---|---|---:|---:|---:|---:|
| Page / 1 KiB | Off | Plaintext | 7,100 | 3,509 | 2.02× | 32.8 / 89.4 |
| One row / 64 KiB | Off | Plaintext | 10,182 | 2,582 | 3.94× | 36.0 / 94.6 |
| Page / 1 KiB | Off | Verified TLS | 6,325 | 2,968 | 2.13× | 35.3 / 91.3 |
| One row / 64 KiB | Off | Verified TLS | 6,970 | 2,251 | 3.10× | 38.4 / 93.9 |
| Page / 1 KiB | On | Plaintext | 7,496 | 4,299 | 1.74× | 32.8 / 85.0 |
| One row / 64 KiB | On | Plaintext | 11,484 | 2,975 | 3.86× | 36.0 / 92.6 |

The uncached four-core campaign completed 72 valid trials including both 8/32
connections and separate borrowing profiles. Across its eight matched copy-mode
cases, ratios were 1.67–3.94×. The separate cache comparison completed 24 valid
trials with fresh uncached controls; its cached ratios are shown above. These
summaries are read from retained historical reports, not newly executed tests.
All retained valid trials reported no HTTP errors; interrupted captures were
excluded and retried. Shared-host noise and three repetitions limit confidence.

API PSS excludes PostgreSQL and the host. These rows show approximately 59–63%
lower API PSS, about 2.5–2.7× smaller working sets, rather than a measured
threefold minimum-memory improvement. Frontend traffic was HTTP. Page RPCs return
100 rows; the large-input RPC returns one row. SQL arguments match; PostgREST's
HTTP request adds its native input wrapper. Both products keep their native SQL
execution. Cache-on profiles allow 1,000 entries and reuse ten tokens; pgxio also
has a 1 MiB claims/digest budget and its own expiration policy. They are not tests
of predominantly unique JWTs or full caches.

These results do not rank other backend products, certify the current snapshot,
or establish a minimum RAM allocation. Raw machine-specific reports remain local.
The public tools support equivalent fixtures; rerun on the revision being evaluated.

## Exploratory observations

Historical development runs used ARM64 Linux containers in Colima on a shared
macOS host. These were not production capacity tests, and the publication snapshot
has not been benchmarked. Two observations motivated the current design:

- In one optional-schema comparison, API proportional set size was approximately
  10 MiB without a contract and 34 MiB with a loaded contract. The database was
  largely saturated and throughput varied substantially; this does not establish
  a minimum RAM requirement or isolate a reliable speedup.
- A separate 60-trial copy/auto buffer comparison used one API core, four database
  cores, 32 clients, pool eight, uncached RS256 JWT, and 1/16/64 KiB inputs. At
  64 KiB with database TLS disabled, auto showed about 4.1% higher median throughput
  and 3.9% lower API CPU per request. Auto copied TLS inputs; differences there
  were within the observed shared-host variation. This was not a PostgREST comparison.

These summaries describe historical local experiments, not guarantees. Raw
machine-specific datasets are not part of this source preview. Reproduce the
experiments on the revision and hardware being evaluated before relying on them.
No universal “2x faster”, minimum-memory or production-support claim is made.

## Measurement requirements

Use equivalent SQL, data, authorization, payloads and connection budgets. Report
frontend and database TLS independently. Match JWT verification and cache settings.
Measure stock libpq separately from the experimental borrowed build.

Collect successful requests/s, errors, p50/p95/p99 latency, API and PostgreSQL CPU,
API PSS/RSS, cgroup memory, and load average. Keep API-only costs separate from
API plus database and host costs. Measure database headroom with the provided
pgbench baseline; a saturated database can hide gateway differences.

Run warmup and repeated trials, retain outliers, pin source/image identities, and
publish the environment and limitations. PSS, RSS, cgroup usage and configured
memory limits are different quantities. Minimum-memory tests and CPU-saturation
experiments are separate from platform compatibility checks.

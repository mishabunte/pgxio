# Borrowed-buffer measurements

The experiment remains opt-in. It reduces body copying, but segmented TLS writes
can cost more CPU than copying, especially for small fragmented payloads. These
local measurements are exploratory, with three randomized repetitions per case.

## Standalone client CPU

All 144 trials completed. Each uses one connection and prepared
`SELECT octet_length($1::text)` against stock PostgreSQL 17.11. Measurement is
three seconds after one second of warmup, with client CPU 0 and database CPU 1
in the shared Linux arm64 Colima VM. CPU time is process CPU, excluding database
execution. This benchmark excludes HTTP, JWT and Rust body capture.

The table compares copied and borrowed parameters in the **same patched libpq**.
Values are median client CPU nanoseconds per payload byte. Percentages compare
those medians; they are not confidence intervals or API-throughput gains.

| Body | Segments | Plaintext: copy → borrow (CPU change) | TLS: copy → borrow (CPU change) |
|---|---:|---:|---:|
| 1 KiB | 1 | 19.182 → 20.607 (+7.4%) | 25.027 → 31.657 (+26.5%) |
| 1 KiB | 16 | 19.210 → 22.856 (+19.0%) | 24.895 → 86.686 (+248.2%) |
| 16 KiB | 1 | 2.079 → 1.510 (-27.4%) | 3.274 → 2.551 (-22.1%) |
| 16 KiB | 16 | 2.059 → 1.559 (-24.3%) | 3.220 → 5.694 (+76.8%) |
| 64 KiB | 1 | 1.187 → 0.436 (-63.3%) | 1.859 → 1.154 (-37.9%) |
| 64 KiB | 16 | 0.949 → 0.444 (-53.2%) | 1.737 → 1.956 (+12.7%) |
| 1 MiB | 1 | 0.647 → 0.199 (-69.2%) | 1.251 → 0.664 (-46.9%) |
| 1 MiB | 16 | 0.673 → 0.128 (-81.0%) | 1.498 → 0.719 (-52.0%) |

Plaintext gather writes avoid a socket call per segment. The secure path retains
each segment's stable retry arguments and writes segments separately, adding
encryption-call and record overhead. This is a plausible explanation for the
fragmented TLS regressions, rather than an isolated measurement of that cost.
The 1 MiB cases exercise generic libpq only; pgxio REST remains capped at 64 KiB.

Complete controls, ranges, SQL request rates and immutable image IDs are in
`artifacts/libpq-microbenchmark-measured/{RESULTS.md,medians.csv,medians.json,
measurements.json,environment.json}`. The unpatched same-source copied control is
retained alongside patched copied and borrowed cases. Raw per-trial logs remain.

The copied controls also vary: at one segment the patched/unpatched median CPU
differences range from -7.1% to +30.2% in plaintext, and -1.2% to +8.8% with TLS.
The +30.2% cell is 64 KiB plaintext; the corresponding 16-segment copied-label
cell is effectively equal. Copied mode sends the same contiguous body regardless
of that segment label. These short-run differences need longer reproduction and
profiling before claiming that the internal patch has no legacy-path cost.

## HTTP comparison

All **108 trials passed**, with zero HTTP errors and valid CPU/RAM samples.
The campaign took 82.4 minutes and used HTTP between wrk and Nginx, with both
plaintext and verified TLS between pgxio and PostgreSQL. The three modes are
unpatched copied, patched copied and patched borrowed, with 1/16/64 KiB bodies,
8/32 connections and three randomized repetitions. Each trial measures 30
seconds after 10 seconds of warmup. SQL, RS256 JWT verification with cache off,
pool 8 and one Nginx worker remain identical. API uses virtual CPU 0, database
CPU 1 and wrk CPUs 2–3. Colima remains at four vCPUs on the 10-core M1 Pro.

These are the **32-connection** medians. Primary comparisons use the same patched
image with borrowing disabled/enabled. CPU change is API CPU milliseconds per
successful request; RAM is median of trial mean process PSS, excluding PostgreSQL.

| DB transport | Body | Unpatched RPS | Patched copy → borrow RPS | API CPU/request change | API RAM MiB: copy → borrow |
|---|---|---:|---:|---:|---:|
| off | 1 KiB | 9238 | 8931 → 9633 (+7.9%) | -9.1% | 31.77 → 31.84 |
| off | 16 KiB | 6725 | 6659 → 6999 (+5.1%) | -14.8% | 32.83 → 32.34 |
| off | 64 KiB | 3662 | 3559 → 3424 (-3.8%) | -24.0% | 36.15 → 33.89 |
| verified TLS | 1 KiB | 8508 | 8403 → 7538 (-10.3%) | +20.8% | 33.18 → 33.28 |
| verified TLS | 16 KiB | 5823 | 5800 → 5371 (-7.4%) | +8.4% | 34.34 → 34.04 |
| verified TLS | 64 KiB | 2923 | 2977 → 2892 (-2.9%) | -13.7% | 38.19 → 35.71 |

At 8 connections, 64 KiB CPU/request falls 25.5% in plaintext and 31.3% with
TLS, saving about 0.9–1.0 MiB API PSS. The 16 KiB results are mixed: plaintext
CPU/request rises 9.0% at 8 connections, while TLS is roughly unchanged there.
Small TLS input is consistently worse: API CPU/request rises about 21% and RPS
falls about 10% at both concurrency levels.

Throughput differences include considerable local variation. For example, the
8-connection 64 KiB TLS copied trials range from 2,267–2,824 RPS and borrowed
trials from 2,199–2,839; their +19.9% median difference is not an isolated
copy-removal speedup. The same-source unpatched/copied control remains in every
case. Patched versus unpatched copied CPU/request differences span -5.7% to
+16.3%, with most cells within about 6%; legacy-path equivalence needs stronger
reproduction before an upstream performance claim.

At 32 connections PostgreSQL uses approximately 98–99% of its assigned core.
Median API utilization across configurations is 33–72%. Removing copies can
reduce API CPU/RAM cost without increasing database-limited throughput. No API
saturation or minimum RAM claim follows from this campaign. The requested
four-core PostgreSQL experiment is deferred until after this baseline; no VM
restart or CPU expansion was performed.

Full per-mode tables, RPS ranges, latency, API/DB CPU/RAM, host load and image IDs
are in `artifacts/borrowed-comparison-final/RESULTS.md`, `medians.csv`,
`report/charts.html` and `report/summary.json`. Source hashes, host topology and
environment snapshots sit beside them. Benchmark containers were removed and
the original benchmark settings restored; existing development containers remain
running.

The opt-in default is appropriate: large bodies can save CPU and memory, while
small/fragmented TLS can regress. Reducing secure-write fragmentation and
evaluating a size/segment fallback policy need separate measurements before
default promotion.


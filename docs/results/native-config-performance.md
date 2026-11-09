# Native configuration and optional validation measurements

Measured 2026-10-18 in an ARM64 Linux Docker VM on a shared macOS host. These are local exploratory measurements, not production capacity estimates.

## Parser comparison

Production keeps serde_json. The benchmark compares full `Value` decoding, a typed envelope retaining `params` as borrowed `RawValue`, and C jsmn tokenization plus field extraction. Optional `Cow` string fields in this serde benchmark still allocate; allocation counts are included in the data. All three return the same selected decoded strings on the valid fixtures.

Median microseconds per operation across five 120-ms samples, after warmup:

| Fixture | serde Value | serde RawValue envelope | jsmn extraction |
|---|---:|---:|---:|
| ws_auth | 0.217 | 0.143 | 0.121 |
| ws_subscribe | 0.380 | 0.168 | 0.131 |
| ws_escapes | 0.642 | 0.272 | 0.338 |
| ws_unsubscribe | 0.203 | 0.112 | 0.073 |
| string_1024 | 0.536 | 0.302 | 0.761 |
| numbers_1024 | 4.865 | 1.304 | 1.974 |
| string_16384 | 2.991 | 2.401 | 10.998 |
| numbers_16384 | 75.295 | 19.348 | 30.168 |
| string_65536 | 10.351 | 8.919 | 42.203 |
| numbers_65536 | 308.255 | 77.138 | 119.548 |
| string_1048576 | 169.024 | 142.395 | 660.938 |
| numbers_1048576 | 4881.961 | 1269.479 | 1926.795 |

jsmn wins on several small unescaped envelopes, but loses to the RawValue envelope on escaped strings and every large fixture here. It also accepts all five malformed probes rejected by serde: invalid literals, leading-zero numbers, multiple roots, trailing commas, and an unpaired surrogate. `JSMN_STRICT` does not supply full JSON validation. The extraction wrapper uses serde for escaped selected strings; this is not a standalone replacement parser.

jsmn reuses preallocated tokens outside timing: about 10 MiB of scratch space for the 1-MiB cases. Raw output copying is a separate task: one allocation, roughly 14–15 microseconds for 1 MiB here, with no parsing. Token-only and copy-only rows are explicitly separated in the [complete parser data](native-config-parser.json).

Compiler: Rust 1.99.1 / LLVM 23.1.1, GCC 14.2.0; serde_json 1.0.154; jsmn commit `25647e692c7906b96ffd2b05ca54c097948e879c`. Container pinned to CPU 0. See the [benchmark source and methodology](../../benchmarks/json-parser/README.md).
## API comparison

Fifteen trials: five profiles × three repetitions, randomized with seed 20261018.
Each trial used a 3-second warmup and 10-second measurement, eight connections,
one Nginx worker and eight query connections. API CPU 0, database CPU 1, generator
CPUs 2–3 in an eight-vCPU, 5.77-GiB VM. RS256 verification was enabled, JWT caching
disabled, database TLS verified. The page workload used 10,000 rows, 100 tenants
and 128-byte payload fixtures. All recorded requests returned HTTP 200 and fixture
checks passed. Other services remained running on the shared host.

Values below are medians of three trials; the throughput range is min–max. Latency
columns are medians of individual trial quantiles, not pooled quantiles. CPU time per
response divides measured API CPU utilization by successful throughput. PSS sums
the API processes' proportional resident memory; full RSS/cgroup/database readings
and image identities are in the [trial data](native-config-api.json).

| Profile | Requests/s (range) | API CPU µs/response | API PSS MiB | p95 ms | p99 ms |
|---|---:|---:|---:|---:|---:|
| baseline | 1462 (1309–1520) | 246.7 | 33.9 | 21.16 | 107.13 |
| raw | 1394 (1286–2041) | 181.4 | 10.0 | 15.09 | 51.37 |
| metadata | 1821 (1812–1831) | 132.0 | 33.8 | 6.46 | 9.39 |
| output-schema | 1776 (1714–2058) | 176.2 | 33.9 | 7.00 | 13.64 |
| both-schemas | 1695 (1554–1798) | 186.8 | 33.9 | 7.23 | 11.31 |

`raw` has no contract and both schema checks off. `metadata` loads the contract
with both checks off. `output-schema` enables only output checks; `both-schemas`
enables both. These four profiles use exactly the same new image.

The no-contract profile used about 10 MiB PSS versus about 34 MiB with a contract.
Both raw and metadata-only modes use the response-copy path without schema checks.
Their differing CPU-per-response results do not establish a reliable CPU ranking.
Loading and checking schemas at startup has a memory cost even when request
validation is disabled.

Throughput is noisy and mostly database-bound: database CPU was generally about 90–97%
of its single assigned core. Individual trial ranges show substantial variability.
Three repetitions on this host do not
establish a reliable throughput ranking or a statistically supported speedup.
Keep the outliers; use these results as exploratory CPU/memory evidence and repeat
on a quiet host for capacity decisions.

The legacy baseline is the preserved `pgxio:libpq-control` image, tagged
`pgxio:native-config-before`. It uses the earlier JSON configuration and mandatory
output validation. Its custom unpatched libpq build differs from the new image's
system libpq build. Consequently, the baseline comparison is descriptive and cannot
isolate the output-path change. Same-image profiles avoid that build difference.

Reproduce with the commands in the [benchmark guide](../../benchmarks/README.md#native-configuration-and-optional-validation),
using `--workloads page --duration 10 --warmup 3` for this shorter local run.
The completed run used a separate named database volume. New work directories
receive their own volumes. The new-image trials used the final normal build,
including the rejected-request body-discard fix, without the benchmark-auth feature.

## Validation

Normal and borrowed builds passed 14 Rust core tests and three allocation tests each.
All 54 benchmark-tooling tests passed, TypeScript type-checking passed, and regenerated
TypeScript/Python declarations matched the checked-in files.

Full production-mode runs passed with validation off, both schema checks on, and
borrowed libpq buffers. These cover configuration/reload failures, HTTPS/WSS,
JWT authorization, queue/pool behavior, cancellation, input forwarding, exact JSONB
response bytes, size/type limits, and deferred COMMIT failure. Sequential, unprepared,
JWT-cache and fail-fast compatibility profiles also passed. The [validation record](native-config-validation.json)
records individual image IDs and which checks preceded the final additional fix.

The compatibility runs exposed a pre-existing HTTP/1.1 bug: an early 401 left request
body bytes unread and corrupted the next pipelined request. The reply path now asks
Nginx to discard unread bodies. A deterministic regression reproduced 401/400 before
the fix and verifies 401/200 afterward. Successful RPC replies skip this operation
because their request body has already been read.

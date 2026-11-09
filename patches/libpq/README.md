# Experimental borrowed libpq

Source: https://github.com/mishabunte/postgres, local branch
`libpq-borrowed-params`. Base and patch order are in `source.json`; patch hashes
are in `SHA256SUMS`. Nothing is pushed or submitted by these tools.

The normal pgxio Dockerfile keeps the system client library. This experiment
builds the client from a pinned PostgreSQL development revision and uses the
ordinary PostgreSQL 17 server. New C declarations are bound privately in Rust;
pq-sys and its existing ABI are unchanged.

## Build and enable

```sh
docker build -f Dockerfile.libpq-borrowed -t pgxio:borrowed-libpq .
```

Configure the native Nginx directive:

```nginx
pgxio_libpq_buf auto; # default; copy | borrowed | auto
```

The default is `auto`; all 60 paired HTTP acceptance trials passed.
`borrowed` requires the experimental build and borrows raw REST body segments,
including over TLS. `auto` also works with system libpq, where it always copies.
With the patched client, auto borrows only when every query connection is known
to be plaintext, the body is at least 16 KiB, and it averages at least 4 KiB per
nonempty segment. TLS, GSS and unknown transport state select copying. Transport
state is cached on connection events; the acquired connection is checked again
before dispatch, with an owned-copy fallback if it became encrypted.

Input-schema validation and WebSockets use the copied path in every mode.
The 64 KiB body limit and file-buffer rejection remain. More than 1,024 nonempty
segments use the copied fallback. The former `pgxio_db_borrowed_buffers` directive
has been removed; replace it with `pgxio_libpq_buf borrowed` or `copy`.

Rust owns descriptors and a worker-local request lease. Its existing held
request keeps bytes alive during queuing; the synchronous cleanup hook closes
any DB connection still referencing the body before Nginx destroys the pool.
After successful flush, normal cancellation/rollback can reuse the connection.
Finalization happens outside worker borrows. Shutdown finishes DB connections
before closing their HTTP requests. No shared locks or worker threads are added.

The optimization removes body-byte copies into Rust and libpq's ordinary output
buffer. It does not remove Nginx's own buffering, TLS encryption buffering,
kernel copies, PostgreSQL parsing or response serialization. Metadata allocations
remain. This is not an end-to-end zero-copy transport.

## C ownership contract

`PQsendQueryParamsBorrowed` and `PQsendQueryPreparedBorrowed` take `PQborrowedParam`
arrays. Each parameter has pointer/length `PQbuffer` segments, a text/binary
format and an explicit NULL flag. Empty values and NULL are distinct. Descriptor
arrays may be reclaimed immediately after the call. Text needs no trailing NUL;
embedded NUL is rejected.

After successful submission, body bytes stay immutable/readable until PQflush
returns zero or connection reset/finish returns. A flush error alone does not
release the obligation. Submission failure retains no new references. Construction
is off-connection; transmission failure closes the transport. Flush completion
does not establish SQL/commit success and no writes are replayed.

Owned headers and borrowed bodies share an ordered queue. Plaintext uses gather
writes; TLS/GSS use stable segment pointers and retry lengths. Transitioning from
a pending ordinary TLS write moves its allocation rather than copying it.
Protocol tracing explicitly falls back to the copied parameter API. Existing
entry points keep copying even when mixed with borrowed requests in a pipeline.

## Validation

```sh
docker build --target libpq -f Dockerfile.libpq-borrowed -t pgxio-libpq:borrowed .
.venv-bench/bin/python tests/run-libpq.py
.venv-bench/bin/python tests/run-libpq.py --faults --output artifacts/libpq-faults
docker build --target libpq -f Dockerfile.libpq-borrowed \
  --build-arg LIBPQ_SANITIZERS=address,undefined -t pgxio-libpq:borrowed-asan .
.venv-bench/bin/python tests/run-libpq.py --image pgxio-libpq:borrowed-asan \
  --sanitized --output artifacts/libpq-asan
.venv-bench/bin/python tests/run-isolated.py --image pgxio:borrowed-libpq \
  --libpq-buf borrowed --production --suite all --output artifacts/borrowed-validation
```

The C suite tests text/binary, NULL/empty, invalid arguments, tracing, prepared and
unprepared queries, mixed pipelines/aborts, an ordinary pending write followed by
borrowed input, partial writes, reset, finish and disconnect. It verifies retained
nodes point into the original payload, then protects the payload against access
after release. `--faults` injects allocation failures and short writes/EINTR/EAGAIN
on Linux. Sanitizers
cover the C library and standalone C client, not the Rust/Nginx binary.

For pending-flush request cleanup tests, compile the test-only shim to a shared
artifact using the libpq build image, then pass it to `run-isolated.py` using
`--fault-library artifacts/libpq-test-faults.so`. These input tests include
HTTP/2, disconnect, early Parse errors before body flush, reload and graceful
shutdown. The isolated Nginx fixture preserves the shim's environment explicitly.
The shim is never linked or
loaded in production. Do not use fault injection or sanitizer builds for timing.

## Matched performance evidence

```sh
# Same source revision without the patch; benchmark-auth is only for fixtures.
docker build --target libpq -f Dockerfile.libpq-borrowed \
  --build-arg APPLY_LIBPQ_PATCHES=0 -t pgxio-libpq:control .
docker build -f Dockerfile.libpq-borrowed --build-arg APPLY_LIBPQ_PATCHES=0 \
  --build-arg CARGO_FEATURES=benchmark-auth -t pgxio:libpq-control .
docker build -f Dockerfile.libpq-borrowed \
  --build-arg CARGO_FEATURES=borrowed-libpq,benchmark-auth -t pgxio:borrowed-libpq .
.venv-bench/bin/python tests/run-libpq.py --benchmark \
  --output artifacts/libpq-microbenchmark
.venv-bench/bin/python benchmarks/bench.py borrowed-compare --execute \
  --collector-host colima --api-cpuset 0 --db-cpuset 1 --wrk-cpuset 2-3 \
  --output artifacts/borrowed-comparison
```

Standalone measurements report client process CPU per byte, SQL request rate and
wall time for prepared text-length queries, including 1 MiB and fragmented input.
They exclude pgxio/JWT and are not API capacity measurements. End-to-end defaults
are 108 randomized trials: three matched builds/modes × three body sizes × two
concurrency levels × two DB TLS modes × three repetitions. All use identical
SQL fixtures. Reports retain latency, API/DB RAM/CPU, host load and image IDs.
Database-bound results cannot establish maximum API capacity or an isolated
copy-removal throughput gain. Default promotion is a separate decision.

`UPSTREAM.md` is an unsent discussion draft. The API and ABI are experimental;
Windows/GSS runtime validation and upstream design review remain distinct from
the local Linux/OpenSSL evidence.

See [VALIDATION.md](VALIDATION.md) for the local checks and lifetime regression.
Completed measurements, including small/fragmented TLS regressions, are in
[RESULTS.md](RESULTS.md). The feature remains disabled by default.

## Auto default acceptance gate

Build the experimental image with `CARGO_FEATURES=borrowed-libpq,benchmark-auth`
and tag it `pgxio:buffer-modes`. The benchmark-only counters verify the actual
selection before each timed trial; ordinary builds do not expose them.

```sh
.venv-bench/bin/python benchmarks/buffer-compare.py --execute \
  --output artifacts/buffer-compare-01
.venv-bench/bin/python benchmarks/summarize-buffers.py \
  artifacts/buffer-compare-01 --output artifacts/buffer-compare-summary
```

The gate measures the same one-row RPC with 1, 16 and 64 KiB input, database TLS
disabled and verified, copy versus auto, five repetitions each. It uses HTTP to
the API, one API core, four PostgreSQL cores, 32 clients, pool eight and uncached
RS256 JWTs. CPU/request and RAM are collected alongside throughput. Keep other
builds and tests idle. Auto must lose no more than 3% median RPS or API
CPU/request in every case. If inconclusive, extend through seven repetitions
using `--repetitions 7 --resume-from artifacts/buffer-compare-01` and a fresh
output directory; pass all source directories to the summarizer. Failed resource
captures must be replaced, not counted as valid trials.

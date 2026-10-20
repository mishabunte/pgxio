# Borrowed buffer chains for libpq and pgxio

Historical implementation plan. Its boolean configuration was replaced by
[buffer modes](libpq-buffer-modes.md); current builds use
`pgxio_libpq_buf auto|borrowed|copy`, defaulting to `auto`.

## Objective and defaults

Opt-in experimental REST forwarding from Nginx-owned in-memory body chains through
a generic libpq C API. Rust owns descriptors, not body bytes. Support plaintext
and TLS; encryption/kernel buffering is outside the zero-copy claim. Ordinary
builds and `db_borrowed_buffers=false` retain the existing copied path.

## Source and build

Use https://github.com/mishabunte/postgres with upstream postgres/postgres.
Feature branch: libpq-borrowed-params, based on upstream master at
af090508018be5ace443ba6e2814500c10842a4d. Keep master clean. Local checkout is
artifacts/postgres-src, ignored by pgxio. Export reproducible patches, source
revision and checksums under patches/libpq. Build client/support libraries only;
the integration server remains PostgreSQL 17. A separate experimental Docker
build and borrowed-libpq Cargo feature select the patched client at link/load
time. No additional submodule or server/wire-protocol changes.

## Public C API and lifetime

Add PQsendQueryParamsBorrowed and PQsendQueryPreparedBorrowed. Parameters describe
text/binary format, SQL NULL independently of empty, and pointer/length segments.
Copy descriptors and protocol metadata; borrow immutable payload bytes without
requiring a trailing NUL. Preserve existing API ownership semantics.

After successful submission, retain bytes until PQflush returns 0 or reset/finish
returns. PQflush -1 alone does not release the obligation. Failed submission
retains no new references; construction must be transactional and a partial-send
failure makes the connection unusable. Flush completion is not SQL success.
No release callbacks in v1.

Use ordered owned/borrowed output segments, checked wire lengths, vectored writes
for plaintext and stable segment writes through the existing secure layer for
TLS/other transports. Preserve retry arguments, SIGPIPE/error handling, tracing,
legacy/borrowed interleaving and pipeline ordering. Copied tracing fallback is
permitted and must be explicit.

## pgxio ownership and cleanup

Add db_borrowed_buffers=false; reject enabling it without experimental support.
Borrow raw REST input only; schema-enabled REST and WebSockets retain copying.
Walk actual body chains, skip empty buffers, scan for embedded NUL, retain the
64 KiB cap and reject file-backed bodies. Above 1,024 nonempty segments fall back
to copying. Add no synchronous file reads, shared locks, or background threads.

Keep request leases until complete flush or connection disposal. Cleanup removes
queued jobs; if a database connection still references the request body, finish
that connection synchronously before Nginx frees the pool. After flushing, normal
nonblocking cancellation/rollback remains. Dispose of DB references before HTTP
finalization on shutdown; finalize only outside worker RefCell borrows. No write
retries. Preserve JWT, transactions, output validation and status mapping.

## Verification and evidence

Standalone libpq coverage: text/binary, NULL/empty, multiple segments, invalid
lengths, prepared/unprepared, legacy interleaving, pipelines/aborts, short writes,
backpressure, interruptions, TLS retries, disconnect/reset/finish. Use fault
injection and sanitizers to establish release and pointer safety. Instrument a
separate diagnostic build to count body copies (excluding TLS internals).

Run pgxio isolated suites with borrowing on/off, including fragmentation/chunking,
HTTP/2, queued expiry, disconnect under pending flush, reload and shutdown.

Compare same-base unpatched/copy, patched/copy, patched/borrowed builds. Standalone
measurements isolate libpq allocations/sending. End-to-end matrix: 1/16/64 KiB,
8/32 connections, plaintext/TLS, 3 randomized repetitions. Record throughput,
latency, allocation/copy counts, API/DB CPU, RAM, host load and immutable identities.
Only claim API capacity if DB/generator headroom exists and API approaches full
CPU; retain database-bound results with limitations.

Prepare separate internal/API patches with tests/docs and a pgsql-hackers design
draft. Keep opt-in regardless of initial performance. No push, mailing-list
submission or default promotion is included. Preserve existing uncommitted work.

## Local outcome

Implemented the opt-in client/API and Nginx leases, exported two reproducible
patches and saved the unsent upstream draft. Standalone fault/sanitizer tests,
the isolated module matrix and benchmark tooling checks pass. Measurements
complete: 144 client trials and 108 HTTP trials, including both DB transports.
See [validation](../../patches/libpq/VALIDATION.md) and
[results](../../patches/libpq/RESULTS.md). The feature stays off by default.
The Mac has 10 cores (8 performance, 2 efficiency); Colima remains at four
vCPUs. A four-core PostgreSQL run was deferred by the user until this one-core
baseline finished. No VM restart, commits, push or upstream submission occurred.

# Draft: libpq borrowed segmented parameters for asynchronous queries

Status: local discussion draft, not sent. Attach patches and measured evidence
after review. Target: pgsql-hackers; PostgreSQL development master, not a stable
branch. No GitHub PR or CommitFest entry has been created.

## Motivation

Applications such as HTTP gateways already own immutable request buffers, often
in multiple segments. Current text parameter APIs require NUL termination and
copy data into libpq's output buffer. This prototype accepts explicit-length
segments and queues their bytes by reference alongside owned protocol metadata.
It does not change the PostgreSQL wire protocol or require a patched server.

## Proposed interface

Two additional entry points mirror PQsendQueryParams/PQsendQueryPrepared but use
an array of segmented parameters with explicit format and NULL flags. libpq owns
copied descriptors. Callers retain immutable payloads until a complete PQflush
or reset/finish, including after a flush error. No completion callback is needed
for this initial connection-level contract. Existing APIs retain copying.

All message sizes are validated and messages are constructed off-connection.
Failed submission retains no new references. A transport failure during
submission disposes of references and makes the connection unusable. Flush
completion is deliberately independent of command/transaction success.

## Implementation and compatibility

An ordered output queue combines owned protocol fragments with borrowed payload
segments. Plaintext gathers segments in one socket call. TLS/GSS retain their
existing encryption paths and stable retry buffers. Pending ordinary output is
moved into the queue without changing its address or shrinking a TLS retry.
Legacy and borrowed commands can interleave in pipelines. Tracing uses the
copied implementation because the current trace decoder expects a contiguous
message. No per-connection thread or new shared synchronization is introduced.

The patch set separates internal queue support from the public API, docs and
tests. Current prototype validation targets Linux/OpenSSL and stock PostgreSQL 17.
The declarations are intentionally proposed for review, not presented as an
agreed upstream API. Windows and GSS runtime coverage are still needed before
claiming complete platform coverage.
The test-only allocation injector currently uses glibc hooks; its platform
selection needs portability work before submission to support musl systems.

## Evidence to attach

- C contract tests: segmented text/binary, NULL/empty, validation, trace,
  prepared/unprepared, mixed pipelines and aborts, backpressure, flush/reset/
  finish/disconnect ownership, and legacy-to-borrowed TLS transition.
- ASan/UBSan and guard-page checks; injected EINTR/EAGAIN and short socket writes.
- Transactional allocation failures with an earlier message already queued,
  including the tracing fallback; small-message pipeline auto-flush.
- pgxio integration exercising request cleanup, HTTP/2, reload and shutdown.
  An early Parse error before body flush is a specific regression test: command
  completion cannot establish payload release. Leases must remain with the
  connection until complete flush or disposal. The earlier implementation fails
  this test, while the corrected implementation passes.
- Same-source unpatched/copy, patched/copy and patched/borrowed client CPU and
  end-to-end measurements, with resource evidence and negative results retained.

## Questions for upstream design review

The local evidence is in [VALIDATION.md](VALIDATION.md) and
[RESULTS.md](RESULTS.md). All 144 client and 108 HTTP trials completed. Large
payloads reduce client/API CPU cost, but small and fragmented TLS can regress.
The one-core HTTP campaign is database-limited and does not establish maximum
API capacity. These mixed results argue for further secure-write and fallback
design work, rather than default promotion. Upstream TAP/Meson and non-glibc,
Windows/GSS validation remain prerequisites for a submission-ready patch set.

Is a connection-level lifetime ending at successful PQflush an acceptable first
API, or would maintainers prefer a different completion mechanism? Are the
segmented parameter types and copied tracing fallback appropriate? What further
transport/platform evidence would make the additional buffering complexity
worth maintaining?

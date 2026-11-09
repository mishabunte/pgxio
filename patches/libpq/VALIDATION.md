# Prototype validation

The experiment stays opt-in. Linux arm64/glibc, OpenSSL and a stock PostgreSQL 17.11
server are the local validation targets. Exported patches apply to the pinned
development revision in `source.json`. No fork changes have been committed,
pushed or submitted upstream.

## Correctness evidence

- `artifacts/libpq-validation-final`: C contracts pass with injected short
  writes, EINTR/EAGAIN and allocation failures, over plaintext and verified TLS.
  Each transport exercises 22 allocation-failure positions. Coverage includes
  binary/text parameters, NULL/empty, mixed pipelines, errors, small-message
  auto-flush, tracing and the legacy-to-borrowed TLS retry transition.
- `artifacts/libpq-validation-final-asan`: both transports pass ASan/UBSan.
  Guard pages also detect payload reads after flush/reset/finish/disconnect.
- Under `artifacts/borrowed-validation`, the `final-raw`, `final-copy`,
  `final-schema`, `final-sequential`, `final-unprepared`, `final-cache`,
  `final-failfast` and `final-stock` runs pass integration, performance-behavior
  and input suites. `final-faults-plain` passes plaintext pending-flush cleanup.
- `artifacts/borrowed-validation/frozen-*`: final experimental image regression
  runs, including verified database TLS, HTTP/2 and injected backpressure.
- Build-time Rust checks: 12 core tests and two allocation tests pass.
  Borrowed input capture allocates descriptors rather than body bytes. This is
  not a count of all request allocations or TLS-internal buffering.
- Benchmark tooling: 44 unit tests pass. Export checksums and canonical C test
  copies match. Make builds the upstream test helpers. The upstream TAP harness,
  Meson registration and Windows/GSS paths have not been executed locally; the
  C client is exercised through the standalone runner.

The test-only allocation injector uses glibc allocator hooks. Its build/TAP
integration needs portability work for other Linux C libraries before upstream
submission; the production library does not depend on these hooks.

The first microbenchmark attempt (`artifacts/libpq-microbenchmark-final`) stopped
at compilation because the partial libpq installation omitted `postgres_ext.h`.
The Docker build now installs the interface support headers through PostgreSQL's
`src/include` target. No timings were collected by that failed attempt. The
packaging fix leaves the validated runtime binaries byte-identical:

| Binary | SHA-256 |
|---|---|
| Module | `ae5cdcc396d00894d3ee898d3860c93812ccf3387c67917e6eb48fb8316d7052` |
| Patched libpq | `0dabeab005d34abd50cd82f5d9f6dca7dc9504cc1e02b088a3ac375a6d19f1f8` |

## Lifetime regression and injector investigation

An early PostgreSQL Parse error can consume command results while later Bind
payloads remain queued. Keeping the request lease on the command is therefore
unsafe. Successful borrowed submissions now move every submitted payload into
connection-owned lifetime tracking, released only after a complete flush or
after `PQfinish`. Request cleanup finds these leases independently of the job.

The test shim sends only the protocol prefix, then blocks subsequent writes.
The earlier image (`artifacts/borrowed-validation/negative-lease-control`) fails
the synchronous cleanup regression. The connection-held implementation passes
(`final-faults-env`, then the frozen-image run). This establishes that the test
distinguishes the two implementations.

The first injector run did not block writes because Nginx removed the test
environment variables when creating workers. The isolated fixture now preserves
them with explicit `env` directives. No production injector configuration was
added. Failure logs are retained under `final-faults`; they are not passing
evidence. Investigation status: DONE.

## Performance evidence

The completed measurements and their regressions are summarized in
[RESULTS.md](RESULTS.md): 144 standalone client trials and 108 HTTP trials.

Run the commands in [README.md](README.md) with fresh output directories. Client
CPU microbenchmarks and HTTP comparisons must run sequentially, with no builds
or fault tests competing for the VM. Reports record their immutable image IDs.
Shared local measurements do not establish minimum RAM or production API
capacity; database and generator headroom must be checked before such claims.

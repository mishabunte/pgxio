# Borrowed versus copy without network

This benchmark isolates the costs behind the 1 KiB borrowed/copy difference.
It uses production Rust `input.rs` and `borrowed.rs`, plus the existing patched
libpq static library. It does not open a socket, execute SQL, parse server
responses, serve HTTP, verify JWTs or initialize Nginx. No production code or
libpq patches are changed.

Three stages identify different costs:

- `input`: construct and destroy `Input::Owned` or `Input::Borrowed` from existing
  chunks. Descriptor creation, body copying and normal byte checks are timed.
- `libpq`: prebuilt parameters, prepared-query serialization and complete flush.
  Input construction is outside timing.
- `combined`: input construction, the actual Rust borrowed-send wrapper or copied
  send path, command parameter metadata, connection-held inputs, flush and release.

Each fragmented input has separately allocated source chunks. The copied libpq
baseline receives a contiguous, terminated value; the combined copied path must
first assemble it. One operation is flushed at a time, with pipeline mode off.
There is no batching, queue contention or backpressure. These choices isolate
outgoing input costs, not the entire production request lifecycle.

The C fixture constructs an internal empty `PGconn` without connection APIs.
Linker wrappers replace `send`/`sendmsg` with a discard sink and abort on socket,
socketpair, connect or receive calls. The fake descriptor is invalidated before
connection destruction. Docker also uses `--network none`. Plaintext retains
libpq's real gather-write traversal. TLS retains the actual libpq/OpenSSL write
path, using TLS 1.3 and `TLS_AES_128_GCM_SHA256`; its handshake uses memory BIOs,
then encrypted records go to a discard BIO. Handshake, peer decryption and output
validation are outside timing. Correctness captures decrypt the records and
verify the Bind payload plus equivalent following messages. Default versus
explicit text format explains a two-byte header difference between modes.

## Build and run

Use the existing `pgxio-libpq:borrowed` development image built from
`Dockerfile.libpq-borrowed --target libpq`. It must include the checked-in patch
series. Dependency downloads happen during build only. The benchmark runtime
has no network access.

```sh
docker build -f benchmarks/synthetic/Dockerfile -t pgxio-synthetic:borrowed .
docker build -f benchmarks/synthetic/Dockerfile.validation -t pgxio-synthetic:validation .
docker run --rm --network none \
  -e ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 \
  -e UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 pgxio-synthetic:validation

.venv-bench/bin/python benchmarks/run-synthetic.py
caffeinate -is .venv-bench/bin/python -u benchmarks/run-synthetic.py --execute \
  --output artifacts/borrowed-synthetic
.venv-bench/bin/python benchmarks/summarize-synthetic.py artifacts/borrowed-synthetic
```

Use a fresh output directory. `--smoke` checks all stages/transports with 1 KiB
inputs and 1/16 segments. The full matrix uses 256 B, 1/4/16/64 KiB; 1/4/16
segments; five repetitions; 100 ms warmup and at least 500 ms measurement, in
randomized order. Input-only runs do not repeat an irrelevant TLS dimension.
The full run has 750 timing trials plus 150 diagnostic cases. Keep competing
benchmarks stopped; default CPU affinity is CPU 0. Setup is excluded from timing.

Timing and diagnostic binaries are separately compiled. Only diagnostics load
the Linux/glibc allocation interposer and count writes, TLS records and requested
allocation bytes. Diagnostic timing fields are null and never enter performance
comparisons. Allocation traffic is not live heap size, RSS or a RAM requirement;
realloc counts its full requested new size. Input-only diagnostics assert one
allocation of either body-size+1 or descriptor-array size.

The standalone crate declares only the opaque `PGconn` and `Oid` FFI types used
by production `borrowed.rs`; it does not replace its implementation. Nginx build
dependencies are deliberately absent. The separate sanitizer image rebuilds
libpq and the C fixture with ASan/UBSan and validates 60 cases; Rust is release
compiled, not sanitizer-instrumented. Existing Rust input correctness tests
remain applicable.

## Read the results

`RESULTS.md`, `summary.csv`, `summary.json` and standalone `charts.html` report
process CPU ns/op, wall ns/op, CPU ns/input-byte, medians, ranges and quartiles.
Borrowed/copy ratios above one favor copying. `timing.jsonl` and
`diagnostics.jsonl` retain every sample. The environment records image identity,
compiler versions, static libpq hash and compiled-source hashes, which the runner
checks against local input/borrowed/fixture sources before measuring.

The in-memory sink excludes kernel network costs, and the TLS sink excludes
peer decryption. Sink callbacks, flush bookkeeping and command recycling remain
part of the measured harness. Small absolute differences should be judged with
the trial ranges. These results explain local cost components; they do not
establish HTTP speedups or a safe production borrowing threshold.

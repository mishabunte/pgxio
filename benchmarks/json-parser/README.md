# JSON parser comparison

Run `cargo run --release --locked --manifest-path benchmarks/json-parser/Cargo.toml > measurements.jsonl`.
The standalone benchmark does not change the production parser. The vendored MIT-licensed
jsmn header is pinned to upstream commit `25647e692c7906b96ffd2b05ca54c097948e879c`
(https://github.com/zserge/jsmn), compiled with `JSMN_STRICT` and `JSMN_PARENT_LINKS`.
Cargo.lock pins Rust dependencies; record `rustc -Vv`, CPU, OS and compiler with results.

`serde_value`, `serde_borrowed` and `jsmn_extract` extract and decode the same envelope
strings (`type`, `id`, `stream`, `token`), ignoring the application payload. Checksums
must match before timing. Borrowing serde retains `params` as RawValue. jsmn extraction
decodes escaped keys and selected values using serde_json. Unknown nested tokens are
skipped by their offsets. These operations are compared on valid inputs; acceptance
of malformed input is reported separately. Tokenization is not full JSON validation.

`jsmn_tokens_only` and `raw_owned_copy` perform different tasks and are labelled separately;
they are not substitutes for extraction/validation. Each case has warmup and five timed
samples. Allocation counts are collected in a separate operation. The allocator wrapper
remains installed with counting disabled during timing. jsmn reuses a worst-case token
buffer allocated outside timing; its retained scratch bytes are reported explicitly.

Synthetic large envelopes exercise strings versus number arrays at 1 KiB to 1 MiB;
only small fixtures correspond to actual WebSocket messages (which have a 16 KiB cap).
This measures parser operations, not HTTP/API performance.

For the Linux container build used by the recorded local run:

```sh
docker build -t pgxio:json-parser-bench benchmarks/json-parser
docker run --rm --cpuset-cpus 0 pgxio:json-parser-bench > measurements.jsonl
docker run --rm --entrypoint cat pgxio:json-parser-bench /compiler.txt
```

See [recorded results](../../docs/benchmarks.md) for the measured
image identity, compiler versions, full samples, and API-level comparison.

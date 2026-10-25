# Experimental borrowed libpq

The normal pgxio image uses system libpq. This optional build uses
[`libpq-borrowed-params`](https://github.com/mishabunte/postgres/tree/libpq-borrowed-params)
in the PostgreSQL fork, pinned to the immutable revision in [source.json](source.json).
The fork branch is based on PostgreSQL development sources; it is not an accepted
upstream API or a supported stable-client release. No patched database server is
required. The publication snapshot has not been built or tested.

The two implementation commits live in the fork. pgxio fetches the patched revision
directly; it does not apply another copy of those patches. Integration fixtures
and diagnostic-only allocation seams remain here. Production libpq has no injected
allocation hooks.

## Build and configure

These commands build images and run embedded checks; they are provided for later
validation and were not executed as part of publication.

```sh
docker build -f Dockerfile.libpq-borrowed -t pgxio:borrowed-libpq .
```

Use `pgxio_libpq_buf copy|borrowed|auto` in Nginx's `http {}` block. The default is
`auto`. With stock libpq, auto always copies and forced borrowed mode is rejected.
With the patched client, auto borrows only raw REST bodies of at least 16 KiB,
averaging at least 4 KiB per nonempty segment, with at most 1,024 segments and all
query connections known to be plaintext. Transport is checked again at dispatch.
TLS, GSS, unknown transports, schema-validated input and WebSockets select copying.
Forced `borrowed` mode exercises supported REST borrowing over plaintext and TLS;
schema checking and WebSockets still copy. Existing size and file-buffer limits apply.

Rust owns descriptors and retains the Nginx request while libpq references body
bytes. Input buffers must stay immutable until successful flush or disconnect.
Cancellation, worker reload and shutdown must close pending borrowed connections
before releasing the request. Flush completion does not imply transaction success.
See the fork's libpq documentation for the C lifetime contract.

## Validation and controls

The [platform runbook](../../docs/platform-support.md) covers stock/patched builds,
TLS, cancellation and fault injection. No complete platform matrix is certified.
The standalone C fixtures can be run later with `tests/run-libpq.py`; the borrowed
workflow is manual. Performance tools are in [benchmarks](../../benchmarks/README.md).

For an unpatched control built from the same upstream base, explicitly select it
and disable the borrowed Rust feature:

```sh
docker build -f Dockerfile.libpq-borrowed \
  --build-arg POSTGRES_REF=af090508018be5ace443ba6e2814500c10842a4d \
  --build-arg CARGO_FEATURES= --build-arg LIBPQ_BORROWED=0 -t pgxio:libpq-control .
```

Use the full patched SHA for reproducible builds, not the moving branch name.
The small historical measurements in [the benchmark summary](../../docs/benchmarks.md)
do not establish a universal benefit from borrowing.

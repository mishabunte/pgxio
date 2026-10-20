# libpq buffer mode implementation

Replace the legacy boolean directive with `pgxio_libpq_buf copy|borrowed|auto`.
No compatibility alias is retained. Forced borrowing requires patched libpq;
auto remains valid with the standard client and selects copying there.

Auto borrows raw REST input only with a patched build, no input-schema
validation, all query connections confirmed plaintext, at least 16 KiB input,
at least 4 KiB per nonempty segment, and no more than 1,024 segments.
Cache transport state on audited connection startup and disconnect; recheck the
actual connection at dispatch and copy a queued candidate if it became encrypted.
Preserve the existing request lease through PQflush completion or disconnect.

Validation covers selector boundaries, transport-cache lifecycle, queued-copy
conversion, native Nginx parsing and removed directives, standard-client fallback,
plaintext/TLS integration, schema fallback, pending-flush cancellation/reload,
and synthetic selection with allocation diagnostics.

Default release gate: paired HTTP copy/auto measurements at 1/16/64 KiB,
database TLS disabled/verified, five repetitions (seven if needed), one API
core, four database cores, 32 clients, pool eight, uncached RS256 JWT.
Require zero HTTP errors, valid resource samples, no more than 3% median RPS
loss and no more than 3% median API CPU/request increase in every case.
Retain copy until this gate passes. Do not substitute synthetic timings for
HTTP evidence. Commands are in patches/libpq/README.md.

## Outcome

The five-repetition HTTP gate passed all six cases (60 valid trials, zero HTTP
errors). Auto became the default. At 64 KiB plaintext the median throughput
improved 4.1% and API CPU/request fell 3.9%; other cases stayed within the 3%
regression limit. Evidence: artifacts/buffer-compare-summary/RESULTS.md and gate.json.
Measured images remain frozen under their original tags; default-only rebuilds
use separate tags. Standard libpq still copies in auto mode.

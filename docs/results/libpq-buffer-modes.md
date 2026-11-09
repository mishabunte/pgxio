# libpq buffer-mode acceptance results

Default decision: **auto**. All 60 paired HTTP trials passed with zero HTTP errors and valid resource captures. Each of six cases met both limits: no more than 3% median RPS loss and no more than 3% median API CPU/request increase. Five repetitions per mode were sufficient.

The same experimental module image served the same one-row RPC with 1, 16 or 64 KiB input. Frontend HTTP, PostgreSQL TLS disabled or verified, one API core, four database cores, 32 clients, pool eight, uncached RS256 JWT, 10-second warmup and 30-second measurement. Actual copy/borrowed selection was asserted before timing.

| DB TLS | Input KiB | Copy RPS | Auto RPS | RPS change | CPU/request change | Copy API PSS MiB | Auto API PSS MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| disable | 1 | 15718 | 15750 | +0.2% | -0.1% | 32.1 | 32.1 |
| disable | 16 | 13303 | 13395 | +0.7% | -1.0% | 33.1 | 32.7 |
| disable | 64 | 10444 | 10871 | +4.1% | -3.9% | 36.1 | 34.2 |
| verify-full | 1 | 13514 | 13617 | +0.8% | -1.1% | 33.6 | 33.6 |
| verify-full | 16 | 10643 | 10708 | +0.6% | -0.6% | 34.7 | 34.7 |
| verify-full | 64 | 7197 | 7141 | -0.8% | +1.1% | 37.6 | 37.5 |

Auto borrows eligible large plaintext input; it copies small bodies and every TLS input. Differences in cases using the same copy path reflect measurement variation, not an optimization benefit. Overlapping ranges and shared-host noise limit the precision of small changes. This is a default-release gate, not a universal speed guarantee or a PostgREST comparison.

Raw evidence: `artifacts/buffer-compare-01`, aggregate `artifacts/buffer-compare-summary/RESULTS.md`, `gate.json` and `report/charts.html`. Original measured image tags remain unchanged; default-only rebuilds have separate tags.

Correctness: core selector/transport-cache/queued-copy tests, standard-client fallback, native configuration rejection, plaintext/TLS integration, schema fallback, forced-borrowed pending-flush fault tests, and 90 synthetic timing/90 diagnostic selection smoke cases passed. The initial stock-client suite had two observer-timing failures during competing builds; its quiet full rerun passed without changing code or assertions. Investigation evidence is retained under `artifacts/buffer-stock-timing-investigation.md`.

After the gate, both default-only rebuilt images passed 18 core and three
allocation tests. The patched image passed all six isolated suites with the
directive omitted, and the standard-client image passed omitted-directive buffer
selection with copying. All 56 Python tooling tests and whitespace checks passed.

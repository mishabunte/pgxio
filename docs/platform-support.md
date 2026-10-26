# Native builds and platform validation

**Partial validation: six of twenty combinations passed.** The tested source
is [127c82b](https://github.com/mishabunte/pgxio/commit/127c82b), with experimental
libpq pinned to `e7cfc38544bd66dbf4c4417328bb4f263184e887`. This documentation-only
update does not change those build/test inputs. These are functional compatibility
results, not production certification, performance measurements or minimum-RAM claims.

## Validation targets

| OS | amd64 stock / patched | ARM64 stock | ARM64 patched |
|---|---|---|---|
| Debian 13 | Pending / Pending | Passed | Passed |
| Ubuntu 24.04 LTS | Pending / Pending | Passed | Passed |
| Ubuntu 26.04 LTS | Pending / Pending | Passed | Passed |
| FreeBSD 14.5 | Pending / Pending | Blocked | Blocked |
| FreeBSD 15.1 | Pending / Pending | Blocked | Blocked |

The six Linux cells passed production builds and smoke checks, all six functional
suites over plaintext and verified TLS, schema-enabled TLS, and minimal read-only
runtime image checks. Patched cells additionally passed C ownership/allocation/write
fault tests over plaintext and TLS and sequential, unprepared, JWT-cache and fail-fast
profiles. The C fault tests exercised 19 allocation failures on each transport.

Source fingerprint used by all six passing cells:
`b2e9bee21d3e7483445c018c2afa63f6f6d5c6ccb8c72b4cad8f5fc056fe28ba`.
Stored per-cell build, package and outcome hashes were rechecked before this update.
Raw logs, fixture credentials and machine-specific artifacts remain local.

## Remaining blockers

FreeBSD ARM64 bootstrap encountered an unavailable rustup installer. A local
standalone-toolchain change installed the exact Rust/Cargo 1.99.1 release and
successfully built Nginx. The following Rust adapter build then exposed a missing
ports-header include path (`pcre2.h`). Those tooling fixes are being verified and
are not included in this documentation-only update. No complete FreeBSD cell has
passed yet.

The first full run stopped before amd64 when host free space fell below the
12 GiB starting guard. A small read-only amd64 container did execute successfully,
but this is only an emulator preflight, not an amd64 platform pass. Validation
continues locally; changed source fingerprints require new matching evidence.

Twenty combinations are required. Linux uses distribution containers; FreeBSD
boots its own kernel in QEMU through a pinned AnyVM wrapper. Emulation establishes
correctness only, not performance. Stock libpq must be version 17 or newer. The
optional patched client is pinned independently in `patches/libpq/source.json`.

## Native build

Install dependencies from `scripts/platforms/bootstrap.sh` in a disposable Linux
container or FreeBSD VM. That script installs packages system-wide and is not a
workstation installer. Builds use Rust 1.99.1, Nginx 1.30.5 and Cargo.lock.

```sh
python3 scripts/platforms/build.py --prefix "$PWD/artifacts/native" --libpq system
python3 scripts/platforms/build.py --prefix "$PWD/artifacts/native-borrowed" --libpq borrowed
```

These commands compile and run embedded Rust checks. They produce `bin/nginx`,
`modules/`, `build.json` and a private `libpq/` directory for patched builds.
Adapt the supplied deployment configuration to the installed paths. Build Nginx
and module together; arbitrary distro Nginx binaries are outside this matrix.

## Running the gate

Install host clients into a venv using `benchmarks/requirements.txt` and
`./sdk/python[django]`. Docker, Buildx, Compose and network access are required.
Inspection without `--execute` does not start containers:

```sh
python3 scripts/test-platforms.py
python3 scripts/test-platforms.py --execute --os debian13 --arch arm64 \
  --output artifacts/platform-debian-arm64
python3 scripts/test-platforms.py --execute --output artifacts/platform-full
```

Each cell checks its OS/CPU, builds the production module, checks authentication
and diagnostic absence, then runs plaintext, TLS and schema-mode suites. Patched
cells add C ownership/allocation/write-fault checks and compatibility profiles.
Linux cells additionally exercise the minimal read-only production runtime image.

Run one local cell at a time. Each VM uses two CPUs and 3 GiB RAM. Keep at least
12 GiB free before a cold cell; a warm pair requires 8 GiB. The guest workload is
stopped below 2 GiB free. Only this run's containers/builders/volumes are cleaned;
other projects are preserved. No Colima restart is performed.

Use a fresh output directory for each attempt. `--resume-from OLD_OUTPUT` retains
only verified passing evidence with matching source, manifest and dependency
identities. Results include logs, package lists, build metadata and outcomes.
Failed or missing cells never count as supported. Local credentials and generated
keys must remain private; they are not release artifacts.

GitHub workflows are manual for the source preview. Enable automatic CI and update
the support table after validation. Native packages, service-manager integration,
minimum RAM, capacity benchmarks, Windows and musl are outside this gate.

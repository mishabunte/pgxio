# Native builds and platform validation

**Source preview: the complete matrix is unverified.** No OS/CPU combination is
certified by this publication. Earlier Debian ARM64 debug runs exercised stock and
patched clients, plaintext/TLS and minimal runtime images, but do not constitute a
complete release gate. FreeBSD kernel boot was demonstrated; full FreeBSD builds
and functional suites remain pending. The snapshot contains runner fixes that
have not been tested.

## Validation targets

| OS | CPU | Clients | Status |
|---|---|---|---|
| Debian 13 | amd64, arm64 | stock, patched | Pending |
| Ubuntu 24.04 LTS | amd64, arm64 | stock, patched | Pending |
| Ubuntu 26.04 LTS | amd64, arm64 | stock, patched | Pending |
| FreeBSD 14.5 | amd64, arm64 | stock, patched | Pending |
| FreeBSD 15.1 | amd64, arm64 | stock, patched | Pending |

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

## Running the gate later

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

# Native platform support

The release gate targets Debian 13, Ubuntu 24.04/26.04 LTS and FreeBSD
14.5/15.1, on amd64 and arm64. Both standard and patched libpq must pass:
20 required combinations. These are validation targets, not a statement that
all combinations have already passed. See [current evidence](results/platform-support.md).

## Native builds

Build Nginx and its module together; an arbitrary distribution Nginx binary is
not covered by this support claim. Rust 1.99.1, Nginx 1.30.6 and Cargo.lock are
shared across targets. Standard libpq must be version 17 or newer. On Ubuntu,
use the official PostgreSQL Apt repository if the default client is older.
FreeBSD uses postgresql17-client/server and the compiler/dependencies from pkg.

Install the packages listed in `scripts/platforms/bootstrap.sh` and Rust 1.99.1.
That bootstrap script installs dependencies system-wide and is intended for
disposable test containers/VMs. It is not a workstation installer.

```sh
python3 scripts/platforms/build.py --prefix "$PWD/artifacts/native" --libpq system --jobs 2
# Experimental patched client, installed privately:
python3 scripts/platforms/build.py --prefix "$PWD/artifacts/native-borrowed" --libpq borrowed --jobs 2
```

The staging prefix contains `bin/nginx`, `modules/`, `build.json` and, for the
patched variant, `libpq/`. Pass `--toolchain-home` if Rust is not installed under
`/opt/pgxio-toolchain`; it contains `cargo/` and `rustup/`. Builds never overwrite
the system libpq. Adapt `deploy/nginx.conf` to the installed module/configuration
paths, provide writable temporary directories and run Nginx with
`bin/nginx -p PREFIX/ -c /absolute/path/nginx.conf`. Check `nginx -t` before startup.
Configure the database/JWKS environment as described in [deployment](deployment.md).

## Docker images and the compatibility gate

The existing Dockerfiles remain available. Distribution-specific images use:

```sh
docker build -f scripts/platforms/Dockerfile --target runtime \
  --build-arg BASE_IMAGE=ubuntu:24.04 --build-arg LIBPQ_VARIANT=system -t pgxio:ubuntu24 .
docker build -f scripts/platforms/Dockerfile --target runtime \
  --build-arg BASE_IMAGE=debian:13-slim --build-arg LIBPQ_VARIANT=borrowed -t pgxio:debian13-borrowed .
```

Matrix inspection is read-only and requires no Docker daemon:

```sh
python3 scripts/test-platforms.py
python3 scripts/test-platforms.py --execute --output artifacts/platform-validation-01
python3 scripts/test-platforms.py --execute --os freebsd15.1 --arch arm64 \
  --libpq borrowed --output artifacts/platform-freebsd-arm64
```

Linux runs in distribution containers. FreeBSD boots a real VM through a pinned
AnyVM Docker wrapper; it does not run FreeBSD executables on the Linux kernel.
Cross-architecture/VM results establish correctness only. No host virtualization
tool installation or Colima restart is performed by the matrix runner.

Run one local cell at a time. Each VM uses two CPUs and 3 GiB RAM; builds use two
jobs. Keep 12 GiB disk headroom before a cold cell (8 GiB for a warm pair).
`--min-free-gib 8` permits a constrained local run. The guest workload is stopped
if host free space falls below 2 GiB. FreeBSD uses pristine base images with
temporary snapshot overlays to avoid duplicating the base disk. Temporary Linux
builders and toolchain images are removed after their cells finish; use
`--keep-toolchains` only for debugging. Existing builders/images are not pruned. The runner preserves other Docker projects. It snapshots build/test inputs,
resolves Linux base images to digests, records package/tool versions and checks
the actual kernel, CPU and Nginx event backend. Use a fresh output directory for
every attempt. Retain passing cells with repeated `--resume-from OLD_OUTPUT`;
changed source/manifest/dependency identities reject reuse. Failed and missing
cells never count as supported.

`tests/run-native.py` provides a native backend for the existing functional
suites. Use only in disposable containers/VMs: it creates private database
clusters and starts PostgreSQL on port 5432 and Nginx on 8080/8443. Allocation
diagnostics use a separate instrumented libpq; production libraries have no
allocation hooks. Production smoke tests check that diagnostic endpoints are absent.

The platform workflow runs every required cell independently and uploads logs
and outcomes. It does not publish images or packages. `.deb`/FreeBSD packages,
service-manager integration and performance comparisons are outside this release gate.

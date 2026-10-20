# Platform validation status

Status: **validation in progress; platform support unverified**.

The implementation targets 20 combinations: Debian 13, Ubuntu 24.04/26.04,
FreeBSD 14.5/15.1; amd64/arm64; standard/patched libpq. No combination has
completed its full release gate yet. Do not advertise the matrix as supported.

On 2026-10-20 the first Debian ARM64 standard build compiled Nginx and the Rust
module, passing 18 core and three allocation tests. Docker then failed during
image export with an I/O error creating a containerd temporary mount.

The initial FreeBSD VM download failed decompression integrity validation.
Retrying failed while reading the runner's image blob from containerd.
Read-only Colima diagnostics confirmed I/O errors on `/dev/vdb1` and an aborted
ext4 journal; the guest filesystem has free space. Host APFS had approximately
16 GiB free. The underlying storage cause is not established by those observations.

After approval, Colima was restarted and image storage became readable again.
The nine original services were restored. Unused build cache was reclaimed
without removing images or volumes. Native fixtures now use TCP-only PostgreSQL
connections because host bind mounts do not support Unix-socket permission changes.
The production smoke test and all six native Debian ARM64 plaintext suites
pass. The six native suites also pass with verified TLS, schema validation,
and the patched client in forced-borrowed mode. Patched-client C ownership tests
pass with 19 injected allocation failures and short-write/EINTR/EAGAIN faults,
over plaintext and TLS.

The minimal Debian ARM64 production runtime image passes all six functional
suites with a read-only filesystem and dropped capabilities. A separate smoke
test confirms JWT enforcement and absence of diagnostic endpoints. These debug
runs establish useful evidence but do not replace a complete release cell.

A real FreeBSD 15.1 ARM64 kernel booted under QEMU. Complete FreeBSD builds and
functional tests remain unverified. An interrupted provisioning attempt exposed
an obsolete Python package name; the bootstrap now selects pip for the installed
default Python. The runner now uses snapshot overlays and stops its own fixture
if host disk space drops below 2 GiB.

The attempted 20-cell run is recorded under `artifacts/platform-matrix-01`.
Both Debian ARM64 variants failed their TLS integration stage because the source
snapshot omitted `bin/pgxio`, which the suite invokes to generate a development
token. No full release cell passed. The run was stopped during the next Ubuntu
cell at the user's request to commit without running tests. The snapshot input
list needs correction before rerunning the matrix.

Evidence retained locally:

- `artifacts/platform-first-build.log`
- `artifacts/platform-freebsd-feasibility.log`
- `artifacts/platform-freebsd-feasibility-02.log`
- `artifacts/platform-storage-diagnostics.txt`
- `artifacts/platform-runtime-preflight/full/outcomes.json`
- `artifacts/platform-runtime-preflight/smoke/outcomes.json`
- `artifacts/platform-borrowed-tls-debug-02.log`
- `artifacts/platform-tooling-tests.log`

After storage recovery, run the commands in [the platform runbook](../platform-support.md).
All 20 combinations must pass before updating this status to supported.

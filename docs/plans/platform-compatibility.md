# Initial-release platform compatibility

Accepted release requirements:

- Native builds plus Linux Docker images; defer .deb/FreeBSD packages.
- Debian 13, Ubuntu 24.04/26.04 LTS, FreeBSD 14.5/15.1.
- amd64 and arm64 on every OS, with standard and patched libpq: 20 cells.
- Build matching Nginx/module pairs, using Rust 1.99.1, Nginx 1.30.5 and Cargo.lock.
- Standard client requires libpq 17+. Patched libpq retains its pinned source and
  patch series and is installed privately; it remains experimental.
- Docker distribution containers cover Linux userspace. QEMU inside a pinned
  Docker runner must boot actual FreeBSD and exercise kqueue.
- Reuse functional suites through Compose/native operator backends. Verify
  production builds separately from selection-diagnostic builds.
- Gate each cell on build tests, configuration, HTTP/WS/JWT/permissions,
  plaintext/verified TLS, schema validation, cancellation/reconnect/reload and
  exact response bytes. Patched cells also require C ownership, allocation and
  partial-write/EINTR/EAGAIN/pending-flush cleanup checks.
- Snapshot inputs, pin container/VM identities and record package/tool versions.
  Resume only verified passing evidence with matching source/dependency identity.
- Missing or failed cells do not count as support. VM timings are not benchmarks.
- Preserve existing projects; do not push, publish or create a release.

Implementation/runbook: [platform support](../platform-support.md).
Current evidence and limitations: [validation status](../results/platform-support.md).

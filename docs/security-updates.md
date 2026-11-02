# Security updates

Review upstream Nginx, PostgreSQL/libpq, OpenSSL and Rust dependency advisories.
Assess affected features and the exact deployed artifacts, including experimental
and diagnostic builds separately.

Download the new Nginx archive and detached signature from nginx.org. Obtain the
release key through the upstream key directory, verify its fingerprint against
upstream key information, and verify the detached signature in an isolated GPG
keyring. Only then record SHA-256 and provenance in scripts/sources.json.
PostgreSQL experimental sources record repository, commit and archive checksum;
this is not an upstream signature. Preserve patch checksum enforcement.

Custom Docker NGINX_VERSION or POSTGRES_REF values require NGINX_SHA256 or
POSTGRES_SHA256 respectively. Checksums are enforced on cached archives too.
Native source changes invalidate extracted trees; patch identities and diagnostic
instrumentation are separate inputs. Use fresh output directories for validation.

Rebuild Nginx and its module together. Updating the host's Nginx package does not
replace pgxio's separately compiled binary. Refresh system dependencies during
rebuild, retain Cargo.lock, and review any deliberate dependency changes.

Native build/runtime inventories live under PREFIX/share/pgxio/. They record the
resolved Cargo graph separately from linked native libraries. They are dependency
evidence, not certification. Native host package lists describe the build host;
minimal runtime images must be inventoried separately before release.

Run the agreed Debian release cell and retain logs and inventory hashes. Before
deployment compare binary identities, check nginx -t, readiness and authenticated
API behavior. Retain the previous verified artifact and configuration for rollback.
Do not publish platform support based on one cell. Wider tests are deferred for
this baseline, including experimental sanitizers and other platform combinations.

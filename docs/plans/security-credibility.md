# Security credibility roadmap

## Objective
Publish deliberate PostgreSQL APIs with fewer server components to maintain,
supported by explicit security boundaries and measured evidence.

## Milestones
1. Release hygiene: update Nginx to 1.30.6, verify source archives before extraction,
   inventory build/runtime dependencies, and document security updates.
2. Security evidence: review C/Rust ownership, session isolation, JWT and SQL
   authorization; extend fuzzing and failure testing.
3. Migration evidence: compare a CRUD/notification service with PostgREST and a
   minimal conventional API on maintenance, resources, and behavior.
4. Adoption validation: recruit three PostgreSQL teams and evaluate migration
   friction and demand for maintained builds/support.

## Initial implementation
Only milestone 1 is authorized for this implementation. Share source versions,
URLs and SHA-256 identities across Docker and native builders. Verify Nginx's
upstream signature before recording its checksum. Record experimental PostgreSQL
commit/archive provenance separately. Verify cached archives before extraction;
invalidate extracted trees when identities change. Require checksums for overrides.
Preserve patch checksums and Cargo lock enforcement.

Generate versioned inventories covering sources, patches, compiler, Cargo graph,
features, binary hashes, runtime packages and linked libraries. Retain them with
build artifacts and CI evidence without exposing a public endpoint. These are
inventories, not security certification. Document advisory triage, verification,
rebuild, checks, deployment verification and rollback.

Fix native snapshots to include bin/pgxio and new build inputs. Preserve historical
results and unverified platform status until the complete platform gate passes.
No API, SQL, authentication or buffer-default changes. Deployment, publication,
scheduled scanning and automated update PRs are deferred.

## Acceptance

Validation is narrowed by user instruction to Debian 13 / ARM64 / standard
libpq, using its existing build and functional release cell. Other platforms,
experimental sanitizers and standalone test runs are deferred. The 20-cell
matrix remains the qualification gate for any broad platform-support claim.

## Status — 2026-11-02

Milestone 1 is partially implemented:

- Nginx 1.30.6 signature verified using its upstream release key; source checksum
  and provenance recorded alongside the experimental PostgreSQL commit/archive.
- Docker/native archive checksum enforcement and native extracted-tree identity
  checks implemented; patch checksums enforced on reuse.
- Native build and host runtime inventories generated and retained in CI evidence.
- Native validation snapshot includes bin/pgxio; security-update runbook added.
- Minimal production-image inventories, inventory validation and additional focused
  regression coverage remain unfinished. Platform support remains unverified.

The single Debian attempt at
`artifacts/security-baseline-debian13-arm64-02` stopped before building because
host disk headroom was below the runner's required 12 GiB. No Debian release cell
passed. Earlier 11 tooling tests passed before inventory changes; that result does
not validate the final change set. Further tests are deferred at user request.

Commit/push is explicitly authorized as a partial delivery. No deployment or
release publication is authorized.

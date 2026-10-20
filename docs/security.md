# Security boundaries

A smaller deployment can reduce dependencies and operational work. It does not prove that a backend is more secure than Node, Django, PostgREST, or OpenResty. Nginx, OpenSSL, libpq, Rust dependencies, the C adapter, SQL functions, and deployment configuration remain part of the trusted system.

The concrete boundaries in this alpha are:

- Runtime database login is `web`; Supabase session-pooler usernames may use `web.PROJECT_REF`, but the authenticated database role must still audit as `web`. No role switching is implemented.
- No runtime migration credentials. The development database's admin password is passed only to the database container. Migration tools should use a separate operator environment.
- Explicitly declared SQL calls, validated identifiers, parameterized JSON inputs, result metadata and optional output-schema checks before commit, bounded request sizes, bounded admission, and local transaction timeouts. Prepared statements and pipelining retain these boundaries; transactions never share a connection concurrently.
- JWT algorithms are restricted to RS256/ES256 with configured JWKS. HS256 requires explicit development mode and a development secret. Issuer, audience, subject, and expiry are required. The caller cannot set the SQL role through JWT claims.
- Stream authorization is distinct from signal routing. Permission is checked before each delivery; private data never travels inside NOTIFY payloads.
- Non-root API container, read-only filesystem, dropped capabilities, loopback development port, and exact browser origin allowlists.

## SQL responsibilities

The example's table owner is not the function executor. `pgxio_executor` is a non-login role with only the required table grants; `web` has only schema usage and explicit function execution. Functions use qualified objects and a fixed search path. PUBLIC execution and CREATE privileges are revoked. Repeat the default-privilege rules for whichever role creates future functions; default privileges belong to the creating role.

REST request JSON is passed as a bound `jsonb` parameter without module schema validation by default. PostgreSQL checks JSON syntax, not required fields, property types, ranges or allowed keys. Functions must explicitly enforce those rules; the example uses generic `PT400` exceptions. Existing deployments that rely on input schemas should set `pgxio_validate_input on;` until their SQL checks are in place. Output and subscription-parameter schema checks are opt-in. WebSocket protocol checks, JWT verification, and per-delivery SQL authorization remain mandatory. A contract file alone does not enable checks. With output checks off, schema-mismatched data may reach clients and associated writes commit; enforce intended behavior in SQL and contract tests.

Every published function must enforce its own caller/tenant policy using the sanitized `request.jwt.claims` value. The example filters by subject and enabled account. Functions can also use carefully designed RLS; remember that table owners, superusers, and BYPASSRLS roles may bypass policies. Do not assume putting a function in the `web` schema makes its SQL safe.

The runtime role audit is a useful guard, not a complete privilege proof. Operators must review direct grants, PUBLIC grants, security-definer functions, database privileges, extension capabilities, and all published SQL. A stolen runtime database password can set arbitrary claims and invoke granted functions, so treat it as a server credential.

## Abuse and availability

Use HTTPS/WSS publicly and verify-full TLS to remote PostgreSQL. Restrict database ingress to the API host where possible. Add Nginx rate limits appropriate to your application's login and query costs. A valid token can consume permitted pool/subscription capacity; size admission limits deliberately.

NOTIFY channels have no application tenant ACL. Bogus notifications can cause extra authorization work; they must never expose rows. Rechecks coalesce outstanding invalidations but are still database queries. Many clients subscribing to the same route can create substantial fan-out cost.

SQL is trusted code. libpq can buffer a large result before the module rejects it; schema limits alone do not bound database result memory. Keep SQL aggregation and returned strings bounded. The C FFI and WebSocket path need continued fuzzing and soak tests. A worker crash is isolated by the Nginx master but disconnects that worker's clients.

The integration suite covers input rejection, cross-user isolation, function privilege separation, expired JWTs, subscription revocation, commit/rollback behavior, invalid-output rollback, cancellation rollback, fragmented/oversized/unmasked frames, preread data, listener loss, and worker reload. Passing it is evidence of those cases, not a security certification.

## Cached tokens and recovered sessions

JWT verification caching is disabled by default. Enabling it caches only successfully verified claims, never database authorization decisions. Token expiry is checked on every cache hit, entries have a maximum 30-second lifetime, and worker reload discards the cache along with the old key set. JWKS keys require unique `kid` values. Permission revocation inside SQL and stream reauthorization remain effective while a token is cached.

A failed or abandoned transaction cannot return its connection to the pool until results are drained, cancellation has completed where needed, ROLLBACK succeeds, and PostgreSQL reports an idle transaction state. Otherwise the connection is discarded. Client disconnection after COMMIT has already been sent still has an uncertain write outcome; cancellation does not provide exactly-once writes. Use SQL idempotency for application retries.

# Deployment

## One VPS, local PostgreSQL

Use the included Compose stack for development. For a public deployment, replace development JWT mode with a JWKS file, set exact browser origins, and terminate HTTPS/WSS in Nginx. Keep PostgreSQL private and use independent operator credentials for migrations. Backups and restore testing belong to the database deployment; pgxio does not manage them.

Build with `docker build -t pgxio:alpha .`. The resulting image contains the matching Nginx executable and module; Rust build tools are excluded. The API runs as UID 10001. Mount configuration read-only and provide writable `/tmp`. Set the file-descriptor limit above `worker_connections` plus database/internal sockets. Budget five DB connections for one default worker and up to ten during worker replacement.

The local example subnet is `172.29.87.0/24`. Change both Compose IPAM and `hostaddr` if it conflicts with a host network. An existing named PostgreSQL volume is never reset by the CLI.

## Supabase database, pgxio on your VPS

The topology is browser/Vercel → public HTTPS/WSS pgxio → PostgreSQL over verified TLS. The browser does not need IPv6: only the API host's database connection does.

1. Provision the `web` runtime role and narrowly scoped functions using operator credentials. Adapt the example SQL to your schema; do not expose the Supabase `postgres` password to pgxio.
2. Use the direct `db.PROJECT_REF.supabase.co:5432` hostname and verify the VPS has working IPv6 routing. A session-mode pooler can be an alternative; it must preserve LISTEN's session. **Transaction-mode pooling is incompatible with the listener.** Supabase's documented connection options are [here](https://supabase.com/docs/guides/database/connecting-to-postgres).
3. Set `PGXIO_DATABASE_URL` with username `web`, database, password, original hostname, `sslmode=verify-full`, and the appropriate CA root file. Resolve a numeric address outside the worker:

   ```sh
   umask 077
   bin/pgxio resolve-url > .pgxio/resolved-database-url
   ```

   The output contains a secret. Feed it to your deployment secret manager or environment file; do not commit it or put it in frontend variables. Keep the original hostname alongside `hostaddr` so libpq verifies hostname and certificate. The default CA path is the Debian system bundle; mount a provider CA bundle and override `sslrootcert` when required.
4. Fetch trusted signing keys:

   ```sh
   bin/pgxio jwks https://PROJECT_REF.supabase.co/auth/v1/.well-known/jwks.json .pgxio/jwks.json
   ```

5. Set production configuration like:

   ```nginx
   pgxio_issuer https://PROJECT_REF.supabase.co/auth/v1;
   pgxio_audience authenticated;
   pgxio_jwks_file /etc/pgxio/jwks.json;
   pgxio_development off;
   pgxio_origin https://app.example.com;
   pgxio_pool_size 4;
   pgxio_max_websockets 1024;
   pgxio_endpoint list_tasks web.list_tasks read_only;
   pgxio_endpoint create_task web.create_task;
   pgxio_stream tasks web.authorize_tasks;
   ```

Use asymmetric Supabase signing keys supported by the module (RS256 or ES256). Legacy HS256 projects require migrating their signing configuration; the alpha deliberately has no production shared-secret switch. The example SQL compares text subjects; adapt provisioning to Supabase Auth UUID subjects and your authorization model.

Docker's default bridge may lack IPv6 egress even when the VPS has it. Verify connectivity from the API network namespace. Configure Docker IPv6 routing, use a suitable host-network deployment, or use a compatible session endpoint. This repository does not silently modify host networking. Supabase session usernames may be `web.PROJECT_REF`; the underlying session must still authenticate as `web` and pass the role audit. Live Supabase/pooler compatibility has not been tested with project credentials.

Address refresh is explicit in this alpha. Re-resolve when the provider address changes, update the secret, and recreate the API container. No synchronous DNS runs in a worker. Multi-address failover and automatic resolver refresh are future work.

## TLS and Vercel

For Nginx-owned TLS, add `listen 8443 ssl`, `ssl_certificate`, and `ssl_certificate_key` to the server and publish host 443 to container 8443. The certificate key must be readable by UID 10001. Set certificate renewal/reload through your existing deployment system. The WSS adapter uses Nginx's TLS callbacks; the integration suite tests that path directly.

If an existing proxy terminates TLS, forward HTTP/1.1 Upgrade and Connection headers to pgxio and set idle timeouts above its heartbeat interval. Restrict the unencrypted backend port to that proxy. Never expose the development listener directly to the internet.

In Vercel/Next.js, the public API URL can be a frontend environment variable. Supply a per-user access-token provider to the TypeScript client. Server-side requests can call the same HTTPS API. Database credentials, migration credentials, and signing private keys never belong in `NEXT_PUBLIC_*`. Allow each intended production/preview origin explicitly; there is no wildcard preview-domain bypass. Public WebSockets connect directly to pgxio, independent of Vercel's function lifecycle.

## Key rotation and migrations

JWKS files are loaded at worker startup. Fetch and atomically replace the file with `bin/pgxio jwks`, then reload Nginx (`nginx -c /etc/nginx/nginx.conf -s reload` inside the container). Mount the containing directory, not a single file inode, if using atomic replacement. Retain old signing keys during the issuer's overlap window. Reload closes subscriptions, whose clients obtain fresh tokens and snapshots. There is no network JWKS fetch in the request path.

Run versioned migrations through a separate operator context:

```sh
# PGXIO_MIGRATION_URL is supplied by the operator's secret manager.
bin/pgxio migrate migrations/002_add_endpoint.sql
```

The CLI uses `psql -X`, ON_ERROR_STOP, and a single transaction. SQL requiring nontransactional operations needs an explicit separate operator procedure. This simple runner does not maintain migration history or detect already-applied migrations; use your existing migration system for that.

## Agent workflow

An agent works locally with the schema, contract, generated types, and tests: initialize → write SQL and schemas → generate → check → test. Operator credentials stay in the development/migration context; generated clients contain only HTTPS endpoint types. `bin/pgxio check` provides a small readiness response suitable for scripts. Production deployment credentials and network changes remain an operator concern; this is not an embedded production agent with administrative access.

## REST input migration

`pgxio_validate_input` defaults to `off`. Set it to `on` to retain module input-schema enforcement; both modes forward the original bytes as a bound JSONB parameter. Move required-field/type/range/unknown-property checks into your SQL functions before selecting SQL-owned validation. Updated example functions reject invalid input with SQLSTATE `PT400`. Existing databases need a migration replacing those function definitions; restarting the image does not apply the bootstrap SQL to an existing volume.

Use UTF-8 database connections (the module requests and verifies `client_encoding=UTF8`). Keep `client_max_body_size 64k` and the supplied `client_body_buffer_size 128k`, which leaves space for ordinary chunk framing. Bodies spooled to temporary files are rejected rather than read synchronously in the event loop; unusually fragmented/chunk-extended input or a smaller custom Nginx body buffer can reach that rejection path. Request-size and schema checks do not establish a total client memory limit.

Optional REST schema checks retain the existing serde_json/jsonschema numeric precision limits. Enforce exact high-precision numeric rules in SQL; forwarding preserves the original numeric text in either mode.


## Configuration migration

This release removes `pgxio_config` and `pgxio.json`. Replace the old directive with `include /etc/pgxio/pgxio.conf;` inside `http {}`, or inline the directives. Use `nginx -t` before reloading. Invalid directive values or contracts reject reload without replacing active workers. JWKS loading, environment credentials, and database readiness are still checked by workers; `nginx -t` does not prove runtime authentication or database readiness.

Prefix old settings with `pgxio_`, use `on`/`off` for booleans, and replace the origins array with repeated `pgxio_origin` directives. Rename `validate_rest_input` to `pgxio_validate_input`; it now also controls subscription parameter schemas. Keep connection/cache limits and units unchanged. `issuer` and `audience` are required. `jwks_file` remains a path to signing keys. Native configuration values are parsed once, not evaluated per request.

Move each endpoint's name/function/read_only mapping into `pgxio_endpoint name web.function [read_only];`. Move each stream's name/authorize mapping into `pgxio_stream name web.authorize;`. Convert the optional contract to version 2, removing `function`, `read_only`, and `authorize` fields. Retain endpoint `name`, `input`, `output` and stream `name`, `input`. A contract cannot publish routes. Unknown entries, duplicate names, invalid schemas, and references fail configuration validation. When validation is enabled, every affected declaration needs a schema entry. An empty schema object explicitly permits any JSON value.

Both `pgxio_validate_input` and `pgxio_validate_output` default to `off`. To retain previous output validation, load the contract and enable `pgxio_validate_output on;`. Enable input validation as well to retain subscription schema checks. `/openapi.json` lists configured routes even without a contract, using unrestricted JSON schemas; loading a contract enriches this document without enabling checks.

Provide the connection string through the service/container environment and preserve it with `env PGXIO_DATABASE_URL;` at Nginx's top level, outside `http {}`. Nginx does not perform shell-style environment substitution. The module reads the variable at worker initialization. Changing a service's environment requires restarting/recreating the service; a configuration reload does not import a new parent environment. libpq also supports a `passfile` connection option or `PGPASSFILE` (preserve that variable with `env PGPASSFILE;`); password files must have restrictive permissions and be readable by the worker.

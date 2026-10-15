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

   ```json
   {
     "contract":"/etc/pgxio/contract.json",
     "issuer":"https://PROJECT_REF.supabase.co/auth/v1",
     "audience":"authenticated",
     "jwks_file":"/etc/pgxio/jwks.json",
     "development":false,
     "origins":["https://app.example.com"],
     "pool_size":4,
     "max_websockets":1024
   }
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

# pgxio

Working title: **pgxio**. Default domain: `pgxio.org`.

Small SQL-first REST and WebSocket backend, implemented as a **Rust Nginx dynamic module** with nonblocking libpq. This is an alpha revival of c2h5oh; the original source is preserved in `legacy/c2h5oh`.

One Nginx worker runs an event loop. The default deployment has a master process and one worker, **not one process total**. There is no Node, Python, Tokio, or Axum runtime in the API process. Rust owns contracts, authentication, queries, and WebSocket state; a small C adapter integrates Nginx callbacks and TLS sockets.

## Run locally

Requires Docker Compose and Python 3.9+. No local Rust installation is needed.

```sh
bin/pgxio init
bin/pgxio up
TOKEN=$(bin/pgxio token alice)
curl http://localhost:8080/v1/create_task \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Hello from pgxio"}'
curl http://localhost:8080/v1/list_tasks \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

`init` creates ignored, random development secrets with mode 0600. The example SQL runs only when the PostgreSQL volume is first created. `bin/pgxio down` preserves that volume. The API is bound to loopback; this development configuration is not a public deployment configuration.

`GET /healthz` checks the process. `GET /readyz` requires an idle audited query connection and an active listener. `GET /openapi.json` describes the explicit HTTP endpoints. Database errors return a generic code rather than SQL, credentials, or private rows. HTTP requests are not queued when the query pool is full: they receive 503.

## SQL contract and permissions

[`examples/contract.json`](examples/contract.json) names published `web.function(jsonb) RETURNS jsonb` endpoints and their input/output schemas. Unlisted functions are never addressable. JSON Schema references are disabled. Invalid output rolls back the transaction before commit.

The runtime logs in as `web`. Every connection checks that it has no superuser, role-management, replication, RLS-bypass, inherited role memberships, or `CREATE` privilege in `web`. Each request uses a parameterized function call inside a transaction, with local JWT claims and timeouts. JWT role claims cannot select a PostgreSQL role.

[`sql/example.sql`](sql/example.sql) separates the table owner, non-login function executor, and runtime role. The runtime has no private table grants. `SECURITY DEFINER` functions have a fixed search path, explicit ownership checks, and revoked PUBLIC execution. This is an example policy, not automatic authorization for arbitrary SQL you add. See [security boundaries](docs/security.md).

## WebSocket subscriptions

Connect to `/v1/events` with subprotocol `pgxio.v1`. Send:

```json
{"type":"auth","token":"JWT"}
{"type":"subscribe","id":"tasks-1","stream":"tasks","params":{}}
```

Wait for `{"type":"ready"}` before subscribing, then `{"type":"subscribed","id":"tasks-1"}` before fetching the first HTTP snapshot. A `{"type":"changed","id":"tasks-1"}` message means refetch. Tokens never appear in URLs. Connections close when tokens expire; reconnect with a fresh token.

Each stream has a SQL authorization function returning an opaque routing string or JSON null. Authorization runs at subscription time and again before each coalesced invalidation. One PostgreSQL LISTEN connection per worker serves all subscribers; sockets do not consume individual database connections. The defaults are four query connections plus one listener, 1,024 WebSockets, and 16 subscriptions per socket.

Notifications are ephemeral. On database loss or worker reload, clients reconnect, resubscribe, and fetch a fresh snapshot. Invalidations during a snapshot trigger another fetch. There is no replay, CDC, presence, arbitrary channel subscription, or WebSocket write RPC in this protocol. [Protocol and limits](docs/protocol.md).

## Node, browser, Python, Django

The clients call endpoints; they do not replace an ORM. Generate endpoint types:

```sh
python3 scripts/generate.py examples/contract.json examples/generated
```

TypeScript source in `sdk/typescript` works in browser bundlers and Node with native `fetch`/`WebSocket` and TypeScript stripping. It is not yet a published npm package.

```ts
import { Pgxio } from './sdk/typescript/src/index.ts';
import type { API, Streams } from './examples/generated/contract.ts';
const db = new Pgxio<API, Streams>('https://api.example.com', getAccessToken);
const stop = db.watch('tasks', {}, {
  snapshot: signal => db.call('list_tasks', {}, signal),
  onData: data => renderTasks(data.tasks),
  onError: console.error,
});
```

```sh
python3 -m pip install './sdk/python[django]'
```

```python
from pgxio import Client
with Client('https://api.example.com', get_access_token) as api:
    tasks = api.call('list_tasks', {})
```

Python also provides `AsyncClient.snapshots`. For Django, set `PGXIO_URL` and use `pgxio.django.client(user_token)` as a context manager. Pass the caller's token explicitly; the integration never creates an admin client or patches Django's ORM.

## Deployment, tests, and measurements

- [VPS, Supabase IPv6, Vercel, key rotation](docs/deployment.md)
- [Architecture and Rust/C/OpenResty tradeoffs](docs/architecture.md)
- [Competitors and agent positioning](docs/competitors.md)
- [Benchmark methodology and measured results](docs/benchmarks.md)
- [Competitive benchmark tools and runbook](benchmarks/README.md) · [test plan](benchmarks/PLAN.md)

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e './sdk/python[django]'
.venv/bin/python tests/integration.py
.venv/bin/python scripts/test-tls.py
node --experimental-strip-types tests/typescript.ts
.venv/bin/python scripts/benchmark.py --requests 1000 --websockets 100
```

The Docker build runs Rust contract/authentication tests. Integration tests intentionally modify example functions, terminate the test listener, and reload the test worker. Run them only against the included local development stack.

The alpha still needs sustained soak/fuzz testing and independent security review. Managed Supabase deployment and a 512 MiB VPS capacity target require separate validation. The earlier standalone experiment remains in `experiments/standalone` and is excluded from the active workspace.

## Database performance settings

Healthy database connections are reused. SQL errors and invalid output roll back before reuse; cancellation uses nonblocking libpq cancellation and bounded recovery. Transport or recovery failure reconnects and repeats the role audit. Output validation still precedes COMMIT, and writes are never automatically retried.

Optional fields in `pgxio.json`:

| Field | Default | Limits / behavior |
|---|---:|---|
| `db_prepared_statements` | `true` | Named settings/function statements per connection; disable for poolers without named-statement support |
| `db_pipeline` | `true` | Batch BEGIN/settings/function, then validate before a separate COMMIT |
| `pool_acquisition_timeout_ms` | `1000` | 0–60,000; zero selects immediate rejection |
| `pool_queue_limit` | `32` | 0–4,096 waiting jobs per worker; zero prevents waiting |
| `jwt_cache_max_entries` | `0` | 0–100,000; zero disables token caching |
| `jwt_cache_max_bytes` | `1048576` | Up to 64 MiB of accounted payload; must be positive when caching is enabled |

A full queue returns HTTP 503 `busy`; acquisition timeout returns 503 `pool_timeout`. Queue memory includes retained requests and parsed JSON, in addition to the job count. JWT cache accounting covers serialized claims plus digests, with additional bounded metadata/allocator overhead. Cached tokens expire within 30 seconds and never bypass SQL permission checks. All pool, queue, and cache budgets multiply by worker count, with temporary overlap during reload.

Build and runtime require **libpq 17 or newer**. This does not require PostgreSQL server 17. For architecture, cancellation semantics, and compatibility controls, see [runtime architecture](docs/architecture.md). For isolated regression tests and stage comparisons, see [benchmark instructions](benchmarks/README.md).

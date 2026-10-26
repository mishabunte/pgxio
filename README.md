# pgxio

![pgxio — PostgreSQL meets the world](docs/assets/pgxio-logo.png)

A small SQL-first REST and WebSocket backend, implemented as a Rust Nginx dynamic
module with nonblocking libpq. Publish explicit PostgreSQL functions, authenticate
requests with JWTs, and subscribe to authorized invalidation signals.

**Experimental source preview.** This is not a production-ready release.
Six Linux ARM64 platform cells have passed: Debian 13 and Ubuntu 24.04/26.04,
with stock and patched libpq. FreeBSD and amd64 validation remain incomplete;
see the [tested revision and platform status](docs/platform-support.md).
There are no release binaries or published SDK packages. All GitHub workflows
remain manual while validation continues.

## Early performance results

**Up to 3.9× PostgREST's throughput, with about 60% lower API memory** in
historical local RPC benchmarks. Across the matched cases, observed throughput
was 1.7–3.9× PostgREST 14.17. These shared-host measurements used one API core,
four PostgreSQL cores and equivalent SQL/JWT fixtures; they are not a guarantee
for this unbenchmarked source preview or a ranking of all backend solutions.
See the [results, cache settings and limitations](docs/benchmarks.md).

## Run locally

Requires Docker with Compose and Python 3. The image builds Nginx and its module
together; do not load the module into an arbitrary distribution Nginx binary.
The Docker build also runs its embedded Rust checks.

```sh
bin/pgxio init
bin/pgxio up
bin/pgxio check
curl http://localhost:8080/v1/list_tasks \
  -H "Authorization: Bearer $(bin/pgxio token alice)" \
  -H 'Content-Type: application/json' --data '{}'
```

Initialization creates ignored development credentials. The API listens on
loopback port 8080. Development HMAC tokens are for local use only. The example
SQL initializes a new database volume; restarting does not migrate existing data.
`bin/pgxio down` preserves the database volume.

## Define SQL endpoints

The included example exposes `web.function(jsonb) RETURNS jsonb` functions through
explicit routes. Unlisted functions are not addressable. Include
[pgxio.conf](examples/pgxio.conf) inside Nginx's `http {}` block:

```nginx
pgxio_issuer pgxio-dev;
pgxio_audience pgxio;
pgxio_endpoint list_tasks web.list_tasks read_only;
pgxio_endpoint create_task web.create_task;
pgxio_stream tasks web.authorize_tasks;
```

PostgreSQL validates incoming JSON syntax. Your SQL must enforce field types,
required fields, ranges, ownership and permissions. Optional JSON Schema checking
is available with `pgxio_contract`, `pgxio_validate_input on`, and
`pgxio_validate_output on`; both validation flags default to `off`. Contract
metadata can enrich OpenAPI and generate client types without enabling checks.
Responses are released only after successful COMMIT.

The [example SQL](sql/example.sql) separates owner, executor and runtime roles.
The runtime has no direct private-table grants. Follow the
[security boundaries](docs/security.md) when adapting these functions.

## WebSockets and clients

`/v1/events` carries authenticated, authorized invalidation signals: clients refetch
snapshots through REST. Signals are ephemeral; this is not durable change-data
capture, replay, or a message queue. See the [protocol](docs/protocol.md).

The repository contains [TypeScript](sdk/typescript/src/index.ts) and
[Python/Django](sdk/python/pgxio/__init__.py) clients, plus generated example types.
They are source packages in this preview, not npm or PyPI releases.

```sh
python3 scripts/generate.py examples/contract.json examples/generated
python3 -m pip install './sdk/python[django]'
```

## Deployment and development

- [Deployment and configuration](docs/deployment.md): JWT/JWKS, TLS termination,
  Nginx settings, migrations, connection pools and optional schema enforcement.
- [Architecture](docs/architecture.md) and [security](docs/security.md).
- [Native builds and platform validation status](docs/platform-support.md).
- [Benchmark methodology and exploratory results](docs/benchmarks.md);
  [reproduction tools](benchmarks/README.md).
- [Contributing](CONTRIBUTING.md) and [private vulnerability reports](SECURITY.md).

The default image uses stock libpq. An optional
[experimental borrowed-buffer client](patches/libpq/README.md) is built from a
pinned commit in the PostgreSQL fork. It needs no patched PostgreSQL server.

**Optional zero-copy request-body forwarding:** patched libpq sends eligible REST
payloads directly from Nginx buffers, avoiding intermediate body copies in Rust
and libpq. This describes application-level input forwarding; responses, TLS
encryption, and kernel networking still involve copies.

`pgxio_libpq_buf auto` copies with stock libpq; patched builds borrow only eligible
large plaintext inputs. `copy` and forced `borrowed` modes are also available.

The project needs complete platform validation, sustained soak/fuzz testing and
independent security review. Minimum RAM, production capacity, managed Supabase
compatibility and broad competitor speedup claims have not been established.

pgxio is a revival of [c2h5oh](https://github.com/mishabunte/c2h5oh).
Source is distributed under the [BSD 3-Clause license](LICENSE). Vendored jsmn
benchmark code retains its MIT notice; PostgreSQL fork sources retain their
upstream license. No PostgreSQL or Nginx endorsement is implied.

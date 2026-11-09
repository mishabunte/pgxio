# Competitors and product direction

Research checked 2026-10-14/15. Features below come from official project documentation; no comparative performance results are inferred from implementation language.

| Product | Relevant strengths | WebSockets/realtime | pgxio angle |
|---|---|---|---|
| [PostgREST](https://docs.postgrest.org/en/stable/) | Mature PostgreSQL-driven REST, filtering, embedding, RPC and permission integration | Its documented [LISTEN facility](https://postgrest.org/en/stable/references/listener.html) reloads schema/configuration; it is not a client subscription service | Narrow explicit JSON function contracts plus authenticated invalidations in the same Nginx worker; measure against RPC with equivalent SQL/auth |
| [OpenResty](https://openresty.org/en/) | Nginx event model, LuaJIT, broad libraries and deployment experience | [lua-resty-websocket](https://github.com/openresty/lua-resty-websocket) provides client/server support | Compete on ready-made SQL contracts, permissions, SDKs and operational simplicity; WebSocket support itself is not unique |
| [Supabase](https://supabase.com/docs/guides/realtime) | Integrated database, auth, storage, tooling; Realtime includes Broadcast, Presence and Postgres Changes | Separate realtime service, substantially broader than invalidations | Complement hosted database/auth with a small self-hosted API; do not claim feature parity with the whole platform |
| Minimal Node/Go API | General application code and familiar libraries; can also be lightweight | Available through application/framework libraries | Compare a minimal implementation, not a large unrelated monolith; pgxio's differentiator is less application-server code to maintain |
| [boat.dev](https://boat.dev/) | Persistent agent sandboxes with CLI, SSH, Docker, snapshots and machine-level workflows | Infrastructure rather than a PostgreSQL API | Reference for agent-friendly provisioning and repeatable environments; potential place to build/test pgxio, not a direct REST competitor |

The first audience is an agent-assisted developer who already wants PostgreSQL and a few deliberate SQL endpoints, and wants to avoid building a general application backend. A useful promise is: “Give your agent a contract and migrations; get a small API you can inspect and run yourself.” Keep production agents away from admin credentials.

The product can connect a Vercel frontend to a pgxio VPS, with either local PostgreSQL or a Supabase database. This is an architectural complement to those services. Vercel and Supabase solve different layers; there is no need to frame the product as replacing both.

The stronger case for security is an explicit function surface, restricted credentials, fixed SQL ownership/search paths, validated inputs/outputs, and testable authentication boundaries. “Node requires gigabytes” and “smaller is automatically more secure” are not reliable comparative claims. Publish measured disk/RAM and a threat model instead.

For benchmarks, separate implementation overhead from product features. REST-only PostgREST should not be charged for an unrelated realtime service; pgxio's listener cost should be included when subscriptions are enabled. OpenResty comparisons need equivalent authorization, PostgreSQL pooling, TLS, result shapes, and notification semantics. Presence or durable replay are not equivalent to ephemeral invalidation.

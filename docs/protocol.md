# Protocol v1

REST endpoints are POST `/v1/{contract endpoint}`, with `Authorization: Bearer ...` and JSON content type. No client-supplied SQL, table name, role, or PostgreSQL channel is accepted. Responses are the function's JSON result. Errors use `{"error":{"code":"..."}}` with HTTP 400, 401, 403, 404, 405, 413, 415, 500, or 503 as appropriate. Pool waiting and rejection follow the configured admission limits.

REST input schemas are descriptive by default; PostgreSQL validates `jsonb` syntax and SQL functions enforce application rules. Set `pgxio_validate_input on;` to add module input-schema checks. Output schema validation defaults to off; `pgxio_validate_output on;` enables it before COMMIT. Both flags require a contract with complete coverage. Successful REST bodies are the original PostgreSQL JSONB text in either mode. The module rejects literal NUL bytes with 400 `invalid_json`. In schema mode, local parsing failures also return `invalid_json` and schema failures return `invalid_input`. PostgreSQL endpoint errors with SQLSTATE `22P02`, `22021`, `22P05`, `22003`, or explicitly raised `PT400` return 400 `invalid_input` after successful rollback; other database failures return 503 `database_error`. Database diagnostics are never returned. Oversized bodies return 413. Input errors now consume a database slot/round trip in the default mode.

WebSocket transport uses HTTP/1.1 Upgrade at `/v1/events`, subprotocol `pgxio.v1`. Browser origins must match configured exact origins. Requests without Origin are allowed for authenticated non-browser clients. URL query parameters are rejected. Extensions, including compression, are not negotiated.

| Client message | Server behavior |
|---|---|
| `{"type":"auth","token":"..."}` | Verify signature, issuer, audience, subject and expiry; return `{"type":"ready"}` |
| `{"type":"subscribe","id":"1","stream":"tasks","params":{}}` | Validate input; execute authorization function with caller claims; acknowledge only after active listener and authorization |
| `{"type":"unsubscribe","id":"1"}` | Remove an inactive subscription; unsubscribe while its authorization is pending closes this alpha connection |
| Any write/RPC/publish command | Close connection |

Subscription IDs are nonempty strings of at most 64 bytes, unique within the connection. Stream authorization returns a JSON string of 1–128 bytes or JSON null. A missing/null/changed routing key denies access. The listener accepts only the fixed `pgxio_events` channel and never relays its payload. A valid notification dirties matching subscriptions; a queued SQL authorization recheck precedes `{"type":"changed","id":"1"}`.

Initial acknowledgement is `{"type":"subscribed","id":"1"}`. Fetch a snapshot only after it. Events before acknowledgement are covered by that snapshot; events during the fetch must cause another fetch. The provided clients implement this order. Reconnect establishes a new subscription and a new snapshot; no event cursor is meaningful.

Limits: 64 KiB HTTP input, 1 MiB HTTP result limit, 16 KiB WebSocket messages/frames, 64 KiB Tungstenite outgoing buffer, 4 KiB initial read buffer, 16 subscriptions/socket, default 1,024 sockets/worker. Nginx request/TLS buffers and kernel socket buffers are additional memory. PostgreSQL result buffering is additional memory. Do not interpret these as a total per-client memory ceiling.

Authentication must arrive within ten seconds. Tokens are not refreshed in place. Expiry is checked before subscribe/delivery and by the one-second maintenance timer. Heartbeats send a ping every 30 seconds, with ten seconds to receive a pong. Invalid frames, invalid control messages, unavailable authorization, and buffer exhaustion close the transport. Client-originated close handshakes are parsed; server policy closes currently terminate the transport without an application-specific close code.

Listener failures close all subscribers in that worker. Reconnect backoff ranges from one to 30 seconds. Client SDK reconnect uses jittered exponential backoff; token providers must return fresh tokens. Writes never retry automatically.

PostgreSQL notifications are not private or durable. Other users in the database can subscribe to channels or produce bogus invalidations. An opaque route is not a secret authorization credential. Use signals only for refetching; enforce confidentiality in the HTTP functions. An application needing guaranteed delivery needs an outbox/replay design, outside this protocol.

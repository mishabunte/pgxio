"""Thin endpoint clients. No ORM, SQL construction, or database passwords."""
import asyncio
import inspect
import json
import os
import random
import ssl
from urllib.parse import quote
import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

class PgxioError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(f"pgxio: {code} ({status})")

def _result(response):
    body = response.json()
    if response.is_error:
        raise PgxioError(response.status_code, body.get("error", {}).get("code", "request_failed"))
    return body

class Client:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token
        self.http = httpx.Client(timeout=10)

    def call(self, endpoint, data):
        token = self.token() if callable(self.token) else self.token
        return _result(self.http.post(f"{self.url}/v1/{quote(endpoint, safe='')}", json=data, headers={"Authorization": f"Bearer {token}"}))

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

class AsyncClient:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token
        self.http = httpx.AsyncClient(timeout=10)

    async def _token(self):
        token = self.token() if callable(self.token) else self.token
        return await token if inspect.isawaitable(token) else token

    async def call(self, endpoint, data):
        return _result(await self.http.post(f"{self.url}/v1/{quote(endpoint, safe='')}", json=data, headers={"Authorization": f"Bearer {await self._token()}"}))

    async def snapshots(self, stream, params, snapshot):
        """Yield fresh snapshots after subscribe/reconnect and every invalidation.

        snapshot is an async zero-argument function. Consumer cancellation closes
        the socket. Invalidations received during a fetch cause another fetch.
        """
        url = self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/v1/events"
        delay = .25
        tls = {"ssl": ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))} if url.startswith("wss:") else {}
        while True:
            try:
                async with connect(url, subprotocols=["pgxio.v1"], max_size=16384, max_queue=16, open_timeout=10, **tls) as ws:
                    await ws.send(json.dumps({"type":"auth", "token":await self._token()}))
                    ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if ready.get("type") != "ready":
                        raise PgxioError(401, "websocket_auth_failed")
                    await ws.send(json.dumps({"type":"subscribe","id":"1","stream":stream,"params":params}))
                    async for message in ws:
                        event = json.loads(message)
                        if event.get("id") == "1" and event.get("type") in ("subscribed", "changed"):
                            delay = .25
                            yield await snapshot()
            except ssl.SSLCertVerificationError:
                raise
            except (OSError, asyncio.TimeoutError, ConnectionClosed, InvalidStatus, PgxioError) as error:
                # A lost listener temporarily rejects new handshakes with 503.
                # Treat that as recoverable, but surface certificate/policy errors.
                status = error.response.status_code if isinstance(error, InvalidStatus) else getattr(error, "status", None)
                if status is not None and status not in (429, 502, 503, 504):
                    raise
                await asyncio.sleep(delay + random.random() * delay / 4)
                delay = min(delay * 2, 15)

    async def close(self):
        await self.http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

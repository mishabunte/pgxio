"""Run against the LOCAL example stack only: .venv/bin/python tests/integration.py."""
import asyncio
import base64
import json
import os
import struct
import ssl
import time
import subprocess
import unittest
import sys
from pathlib import Path
import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed
from pgxio import Client, AsyncClient

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("PGXIO_TEST_URL", "http://127.0.0.1:8080")
COMPOSE = ["docker", "compose"]
if os.environ.get("PGXIO_TEST_COMPOSE_FILE"):
    COMPOSE += ["--project-name", os.environ.get("PGXIO_TEST_PROJECT", "pgxio-perf-test"),
                "--file", os.environ["PGXIO_TEST_COMPOSE_FILE"]]
if os.environ.get('PGXIO_TEST_BACKEND') == 'native':
    COMPOSE = [sys.executable, str(ROOT / 'tests/native-operator.py')]
WS = URL.replace("https://", "wss://").replace("http://", "ws://") + "/v1/events"

def connect(url, **kwargs):
    if url.startswith("wss:"):
        kwargs["ssl"] = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))
    return ws_connect(url, **kwargs)

def token(subject="alice", seconds=3600):
    if os.environ.get("PGXIO_TEST_RSA_KEY"):
        enc=lambda v:base64.urlsafe_b64encode(v).rstrip(b"=")
        header=enc(b'{"alg":"RS256","kid":"local-test","typ":"JWT"}')
        payload=enc(json.dumps({"sub":subject,"exp":int(time.time())+seconds,"iss":"pgxio-dev","aud":"pgxio"}).encode())
        message=header+b"."+payload
        signature=subprocess.check_output(["openssl","dgst","-sha256","-sign",os.environ["PGXIO_TEST_RSA_KEY"]],input=message)
        return (message+b"."+enc(signature)).decode()
    return development_token(subject, seconds)

def development_token(subject="alice", seconds=3600):
    if os.environ.get("PGXIO_TEST_DEV_SECRET"):
        import hashlib, hmac
        enc=lambda value:base64.urlsafe_b64encode(value).rstrip(b"=")
        message=enc(b'{"alg":"HS256","typ":"JWT"}')+b"."+enc(json.dumps({"sub":subject,"exp":int(time.time())+seconds,"iss":"pgxio-dev","aud":"pgxio"}).encode())
        return (message+b"."+enc(hmac.new(os.environ["PGXIO_TEST_DEV_SECRET"].encode(),message,hashlib.sha256).digest())).decode()
    return subprocess.check_output(["python3", str(ROOT / "bin/pgxio"), "token", subject, "--seconds", str(seconds)], text=True).strip()

def sql(statement):
    return subprocess.check_output(COMPOSE + ["exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "pgxio", "-At", "-v", "ON_ERROR_STOP=1"], input=statement, text=True, cwd=ROOT).strip()

async def subscribed(subject="alice", seconds=3600):
    ws = await connect(WS, subprotocols=["pgxio.v1"], origin="http://localhost:3000")
    await ws.send(json.dumps({"type":"auth", "token":token(subject,seconds)}))
    assert json.loads(await asyncio.wait_for(ws.recv(), 3))["type"] == "ready"
    await ws.send(json.dumps({"type":"subscribe", "id":"s", "stream":"tasks", "params":{}}))
    assert json.loads(await asyncio.wait_for(ws.recv(), 3)) == {"type":"subscribed", "id":"s"}
    return ws

class Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.http = httpx.AsyncClient(base_url=URL, timeout=8)
        for _ in range(60):
            try:
                if (await self.http.get("/readyz")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.25)
        self.fail("API did not become ready")

    async def asyncTearDown(self):
        await self.http.aclose()
        # Older CPython SSL transports finish shutdown on a subsequent loop tick.
        await asyncio.sleep(.25)

    async def call(self, name, data, subject="alice"):
        return await self.http.post("/v1/"+name, json=data, headers={"Authorization":"Bearer "+token(subject)})

    async def test_http_auth_schema_and_isolation(self):
        self.assertEqual((await self.http.get("/healthz")).status_code, 200)
        self.assertEqual((await self.http.post("/v1/list_tasks",json={})).status_code,401)
        self.assertEqual((await self.http.post("/v1/list_tasks",json={},headers={"Authorization":"Bearer "+token(seconds=-10)})).status_code,401)
        self.assertEqual((await self.call("create_task", {"title":""})).status_code,400)
        self.assertEqual((await self.call("list_tasks", {"sql":"select 1"})).status_code,400)
        self.assertEqual((await self.http.get("/healthz",headers={"Origin":"https://evil.example"})).status_code,403)
        created=await self.call("create_task",{"title":"alice private"})
        self.assertEqual(created.status_code,200,created.text)
        bob=await self.call("list_tasks",{},"bob")
        self.assertEqual(bob.status_code,200,bob.text)
        self.assertNotIn(created.json()["id"], [t["id"] for t in bob.json()["tasks"]])
        self.assertEqual((await self.http.get("/openapi.json")).json()["openapi"],"3.1.0")
        if os.environ.get("PGXIO_TEST_RSA_KEY"):
            rejected_token=development_token()
            rejected=await self.http.post("/v1/list_tasks",json={},headers={"Authorization":"Bearer "+rejected_token})
            self.assertEqual(rejected.status_code,401)

    async def test_web_role_cannot_read_tables_or_create(self):
        self.assertEqual(sql("SELECT has_schema_privilege('web','pgxio_private','USAGE'),has_table_privilege('web','pgxio_private.tasks','SELECT'),has_schema_privilege('web','web','CREATE');"),"f|f|f")

    async def test_commit_rollback_and_cross_user(self):
        alice,bob=await subscribed(),await subscribed("bob")
        try:
            sql("BEGIN; SELECT pg_notify('pgxio_events',route::text) FROM pgxio_private.accounts WHERE subject='alice'; ROLLBACK;")
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(alice.recv(),.3)
            response=await self.call("create_task",{"title":"notification"})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(json.loads(await asyncio.wait_for(alice.recv(),3)),{"type":"changed","id":"s"})
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(bob.recv(),.3)
            sql("BEGIN; SELECT pg_notify('pgxio_events',route::text) FROM pgxio_private.accounts WHERE subject='alice'; COMMIT;")
            self.assertEqual(json.loads(await asyncio.wait_for(alice.recv(),3))["type"],"changed")
        finally:
            await alice.close();await bob.close()

    async def test_revocation_rechecks_before_delivery(self):
        ws=await subscribed()
        try:
            sql("BEGIN; UPDATE pgxio_private.accounts SET enabled=false WHERE subject='alice'; SELECT pg_notify('pgxio_events',route::text) FROM pgxio_private.accounts WHERE subject='alice'; COMMIT;")
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(),3)
        finally:
            sql("UPDATE pgxio_private.accounts SET enabled=true WHERE subject='alice';")
            await ws.close()

    async def test_expiry_and_malformed_control(self):
        ws=await subscribed(seconds=2)
        with self.assertRaises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(),4)
        await ws.close()
        ws=await subscribed()
        await ws.send(json.dumps({"type":"publish","channel":"private"}))
        with self.assertRaises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(),3)
        await ws.close()
        self.assertEqual((await self.http.get("/healthz")).status_code,200)

    async def test_listener_loss_closes_and_recovers(self):
        ws=await subscribed()
        sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='web' AND query='LISTEN pgxio_events';")
        with self.assertRaises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(),4)
        await ws.close()
        for _ in range(40):
            if (await self.http.get("/readyz")).status_code==200:
                break
            await asyncio.sleep(.25)
        ws=await subscribed();await ws.close()

    async def test_worker_reload_closes_subscribers(self):
        ws=await subscribed()
        subprocess.run(COMPOSE+["exec","-T","api","nginx","-c","/etc/nginx/nginx.conf","-s","reload"],cwd=ROOT,check=True,capture_output=True)
        with self.assertRaises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(),8)
        await ws.close()
        for _ in range(40):
            if (await self.http.get("/readyz")).status_code==200:
                break
            await asyncio.sleep(.1)
        else:
            self.fail("replacement worker did not become ready")
        logs=subprocess.check_output(COMPOSE+["logs","--no-color","api"],cwd=ROOT,text=True)
        self.assertNotIn("[alert]",logs,"worker leaked sockets or crashed during the suite")

    async def test_python_and_django_clients(self):
        async with AsyncClient(URL,lambda:token()) as client:
            self.assertIn("tasks",await client.call("list_tasks",{}))
            snapshots=client.snapshots("tasks",{},lambda:client.call("list_tasks",{}))
            try:
                self.assertIn("tasks",await asyncio.wait_for(snapshots.__anext__(),3))
                await client.call("create_task",{"title":"sdk"})
                self.assertIn("tasks",await asyncio.wait_for(snapshots.__anext__(),3))
                sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='web' AND query='LISTEN pgxio_events';")
                self.assertIn("tasks",await asyncio.wait_for(snapshots.__anext__(),6))
            finally:
                await snapshots.aclose()
        def sync_clients():
            with Client(URL,token()) as client:
                assert "tasks" in client.call("list_tasks",{})
            from django.conf import settings
            if not settings.configured:
                settings.configure(PGXIO_URL=URL)
            from pgxio.django import client
            with client(token()) as c:
                assert "tasks" in c.call("list_tasks",{})
        await asyncio.to_thread(sync_clients)

    async def test_fragmented_and_oversized_messages(self):
        async with connect(WS,subprotocols=["pgxio.v1"]) as ws:
            auth=json.dumps({"type":"auth","token":token()})
            await ws.send([auth[:20],auth[20:]])
            self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(),3))["type"],"ready")
            await ws.send("x"*16385)
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(),3)

    async def test_preread_and_unmasked_frame(self):
        # Send handshake and first masked frame in one write. A protocol adapter
        # that discards Nginx's header preread buffer will hang here.
        from urllib.parse import urlsplit
        import ssl
        url=urlsplit(URL)
        context=ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE")) if url.scheme=="https" else None
        reader,writer=await asyncio.open_connection(url.hostname,url.port,ssl=context)
        try:
            data=json.dumps({"type":"auth","token":token()}).encode()
            mask=b"test"
            frame=b"\x81\xfe"+struct.pack("!H",len(data))+mask+bytes(v^mask[i%4] for i,v in enumerate(data))
            headers=("GET /v1/events HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Protocol: pgxio.v1\r\n\r\n").encode()
            writer.write(headers+frame);await writer.drain()
            self.assertTrue((await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),3)).startswith(b"HTTP/1.1 101 "))
            head=await asyncio.wait_for(reader.readexactly(2),3)
            body=await reader.readexactly(head[1]&127)
            self.assertEqual(json.loads(body),{"type":"ready"})
            writer.write(b"\x81\x02{}");await writer.drain()  # invalid unmasked client frame
            self.assertEqual(await asyncio.wait_for(reader.read(1024),3),b"")
        finally:
            writer.close();await writer.wait_closed()

    async def test_invalid_output_rolls_back_write(self):
        original=sql("SELECT pg_get_functiondef('web.create_task(jsonb)'::regprocedure);")
        before=sql("SELECT count(*) FROM pgxio_private.tasks;")
        try:
            sql("""CREATE OR REPLACE FUNCTION web.create_task(input jsonb) RETURNS jsonb
            LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pgxio_private AS $$
            BEGIN
              INSERT INTO pgxio_private.tasks(subject,title) VALUES ('alice','must rollback');
              RETURN '{"invalid":true}'::jsonb;
            END $$;""")
            checking = os.environ.get("PGXIO_TEST_OUTPUT_VALIDATION", "off") == "schema"
            response = await self.call("create_task", {"title":"anything"})
            self.assertEqual(response.status_code, 503 if checking else 200)
            self.assertEqual(int(sql("SELECT count(*) FROM pgxio_private.tasks;")), int(before) + (0 if checking else 1))
        finally:
            sql(original)

    async def test_disconnect_rolls_back_active_query(self):
        original=sql("SELECT pg_get_functiondef('web.create_task(jsonb)'::regprocedure);")
        before=sql("SELECT count(*) FROM pgxio_private.tasks;")
        try:
            sql("""CREATE OR REPLACE FUNCTION web.create_task(input jsonb) RETURNS jsonb
            LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pgxio_private AS $$
            BEGIN
              INSERT INTO pgxio_private.tasks(subject,title) VALUES ('alice','disconnected');
              PERFORM pg_sleep(2);
              RETURN jsonb_build_object('id','test','title','disconnected','done',false);
            END $$;""")
            with self.assertRaises(httpx.ReadTimeout):
                await self.http.post("/v1/create_task",json={"title":"test"},headers={"Authorization":"Bearer "+token()},timeout=.2)
            await asyncio.sleep(2.2)
            self.assertEqual(sql("SELECT count(*) FROM pgxio_private.tasks;"),before)
        finally:
            sql(original)

    async def test_fanout_keeps_http_responsive(self):
        sockets=[]
        try:
            for _ in range(32):
                sockets.append(await subscribed())
            response=await self.call("create_task",{"title":"fanout"})
            self.assertEqual(response.status_code,200,response.text)
            events=await asyncio.gather(*(asyncio.wait_for(ws.recv(),5) for ws in sockets))
            self.assertTrue(all(json.loads(e)["type"]=="changed" for e in events))
            self.assertEqual((await self.http.get("/healthz")).status_code,200)
            # Pool size is independent of client count (four queries + listener).
            self.assertEqual(sql("SELECT count(*) FROM pg_stat_activity WHERE usename='web';"),"5")
        finally:
            await asyncio.gather(*(ws.close() for ws in sockets))

if __name__ == "__main__":
    unittest.main(verbosity=2)

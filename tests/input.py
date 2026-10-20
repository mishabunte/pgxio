"""REST byte ownership, optional schema validation and SQL error recovery."""
import asyncio
import json
import os
import subprocess
import time
import unittest
import httpx
from integration import URL, WS, connect, sql, token, COMPOSE, ROOT
from websockets.exceptions import ConnectionClosed

SCHEMA = os.environ.get('PGXIO_TEST_INPUT_VALIDATION') == 'schema'


class Input(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = httpx.AsyncClient(base_url=URL, timeout=10)
        self.headers = {'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json'}

    async def asyncTearDown(self):
        await self.http.aclose()

    async def call(self, body, endpoint='input_probe'):
        return await self.http.post('/v1/' + endpoint, content=body, headers=self.headers)

    async def test_websocket_application_schema_is_optional(self):
        async with connect(WS, subprotocols=['pgxio.v1'], origin='http://localhost:3000') as socket:
            await socket.send(json.dumps({'type':'auth','token':token()}))
            self.assertEqual(json.loads(await socket.recv())['type'],'ready')
            await socket.send(json.dumps({'type':'subscribe','id':'schema-check',
                'stream':'tasks','params':{'extra':'accepted only without application schema'}}))
            if SCHEMA:
                with self.assertRaises(ConnectionClosed):
                    await asyncio.wait_for(socket.recv(),3)
            else:
                self.assertEqual(json.loads(await asyncio.wait_for(socket.recv(),3)),
                    {'type':'subscribed','id':'schema-check'})

    async def test_rejected_body_does_not_corrupt_pipelined_request(self):
        # Both requests arrive together, including the rejected request's body.
        # A keep-alive response must discard the rejected body before reuse.
        import socket
        import ssl
        import re
        from urllib.parse import urlsplit
        url=urlsplit(URL)
        jwt=token()
        def exchange():
            connection=socket.create_connection((url.hostname,url.port or 80),timeout=5)
            if url.scheme=='https':
                connection=ssl.create_default_context(cafile=os.environ.get('SSL_CERT_FILE')).wrap_socket(connection,server_hostname=url.hostname)
            with connection:
                def request(auth, close=False):
                    return (f'POST /v1/input_probe HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {auth}\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: '+('close' if close else 'keep-alive')+'\r\n\r\n{}').encode()
                connection.sendall(request('invalid')+request(jwt,True))
                response=b''
                while chunk:=connection.recv(65536): response+=chunk
                return response
        response=await asyncio.to_thread(exchange)
        statuses=re.findall(rb'HTTP/1\.1 (\d{3})',response)
        self.assertEqual(statuses,[b'401',b'200'],response)

    async def pid(self):
        response = await self.call(b'{}')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['pid']

    async def operator(self, *args):
        return await asyncio.to_thread(subprocess.run, COMPOSE + list(args),
            cwd=ROOT, check=True, capture_output=True, timeout=20)

    async def recovered(self):
        for _ in range(80):
            try:
                response = await self.call(b'{}')
                if response.status_code == 200: return response.json()['pid']
            except httpx.HTTPError: pass
            await asyncio.sleep(.1)
        self.fail('database pool did not recover')

    @unittest.skipUnless(os.environ.get('PGXIO_TEST_FAULTS') == '1' and
        os.environ.get('PGXIO_TEST_BORROWED') == '1' and not SCHEMA, 'requires borrowed buffers and syscall shim')
    async def test_pending_flush_disconnect_closes_connection_before_pool_free(self):
        previous = await self.pid()
        before = sql('SELECT last_value FROM pgxio_private.input_calls')
        await self.operator('exec','-T','api','touch','/tmp/pgxio-test-block-db-writes')
        pending = asyncio.create_task(self.call(b'{"text":"' + b'x'*60000 + b'"}'))
        try:
            await asyncio.sleep(.2)
            self.assertFalse(pending.done())
            self.assertEqual(sql('SELECT last_value FROM pgxio_private.input_calls'), before)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError): await pending
            for _ in range(40):
                present = await asyncio.to_thread(sql, f'SELECT count(*) FROM pg_stat_activity WHERE pid={previous}')
                if present == '0': break
                await asyncio.sleep(.05)
            else: self.fail('connection retained borrowed body after client cleanup')
        finally:
            pending.cancel()
            await self.operator('exec','-T','api','rm','-f','/tmp/pgxio-test-block-db-writes')
        self.assertNotEqual(await self.recovered(), previous)

    @unittest.skipUnless(os.environ.get('PGXIO_TEST_FAULTS') == '1' and
        os.environ.get('PGXIO_TEST_BORROWED') == '1' and not SCHEMA and
        os.environ.get('PGXIO_TEST_PROFILE') not in ('sequential','unprepared'), 'requires pipelined borrowed buffers and syscall shim')
    async def test_parse_error_keeps_lease_until_pending_output_is_disposed(self):
        previous = await self.pid()
        await self.operator('exec','-T','api','touch','/tmp/pgxio-test-block-db-writes')
        pending = asyncio.create_task(self.call(b'{"text":"' + b'x'*60000 + b'"}', 'borrowed_missing'))
        try:
            for _ in range(40):
                parsed = await asyncio.to_thread(sql, f"SELECT count(*) FROM pg_stat_activity WHERE pid={previous} AND query LIKE '%borrowed_missing%' AND wait_event='ClientRead'")
                if parsed == '1': break
                await asyncio.sleep(.05)
            else: self.fail('parse-error prefix was not delivered ahead of the blocked body')
            await asyncio.sleep(.1)
            self.assertFalse(pending.done(), 'body and sync should still be blocked')
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError): await pending
            deadline = time.monotonic() + .75
            while time.monotonic() < deadline:
                present = await asyncio.to_thread(sql, f'SELECT count(*) FROM pg_stat_activity WHERE pid={previous}')
                if present == '0': break
                await asyncio.sleep(.01)
            else: self.fail('early parse result released the body lease before synchronous cleanup')
        finally:
            pending.cancel()
            await self.operator('exec','-T','api','rm','-f','/tmp/pgxio-test-block-db-writes')
        await self.recovered()

    @unittest.skipUnless(os.environ.get('PGXIO_TEST_FAULTS') == '1' and
        os.environ.get('PGXIO_TEST_BORROWED') == '1' and not SCHEMA, 'requires borrowed buffers and syscall shim')
    async def test_reload_with_pending_borrowed_body(self):
        await self.pid()
        await self.operator('exec','-T','api','touch','/tmp/pgxio-test-block-db-writes')
        pending = asyncio.create_task(self.call(b'{"text":"' + b'x'*60000 + b'"}'))
        try:
            await asyncio.sleep(.2)
            self.assertFalse(pending.done())
            await self.operator('exec','-T','api','nginx','-s','reload','-c','/etc/nginx/nginx.conf')
            try:
                response = await asyncio.wait_for(pending, 5)
                self.assertEqual(response.status_code, 503)
            except httpx.HTTPError: pass
        finally:
            pending.cancel()
            await self.operator('exec','-T','api','rm','-f','/tmp/pgxio-test-block-db-writes')
        await self.recovered()

    @unittest.skipUnless(os.environ.get('PGXIO_TEST_FAULTS') == '1' and
        os.environ.get('PGXIO_TEST_BORROWED') == '1' and not SCHEMA, 'requires borrowed buffers and syscall shim')
    async def test_z_shutdown_with_pending_borrowed_body(self):
        await self.pid()
        await self.operator('exec','-T','api','touch','/tmp/pgxio-test-block-db-writes')
        pending = asyncio.create_task(self.call(b'{"text":"' + b'x'*60000 + b'"}'))
        try:
            await asyncio.sleep(.2)
            self.assertFalse(pending.done())
            started = time.monotonic()
            await self.operator('stop','--timeout','10','api')
            self.assertLess(time.monotonic()-started, 8, 'worker required forced shutdown')
            try:
                response = await asyncio.wait_for(pending, 3)
                self.assertEqual(response.status_code,503)
            except httpx.HTTPError: pass
        finally:
            pending.cancel()
            await self.operator('up','--detach','api')
        await self.recovered()

    @unittest.skipUnless(URL.startswith('https:') and os.environ.get('PGXIO_TEST_HTTP2') == '1', 'requires HTTP/2 fixture')
    async def test_http2_concurrent_body_lifetimes(self):
        async with httpx.AsyncClient(base_url=URL, http2=True, timeout=15) as client:
            async def call(index):
                text = f'stream {index} ' + 'x'*16000
                response = await client.post('/v1/input_probe', json={'text':text},headers=self.headers)
                self.assertEqual(response.http_version, 'HTTP/2')
                self.assertEqual(response.status_code,200,response.text[:100])
                self.assertEqual(response.json()['text'],text)
            await asyncio.gather(*(call(i) for i in range(8)))

    async def test_bad_json_never_calls_function_and_connection_is_reused(self):
        pid = await self.pid()
        before = sql('SELECT last_value FROM pgxio_private.input_calls')
        for body in [b'', b'{', b'{"x":tru}', b'{} {}', b'{"text":"\xff"}',
                     b'{}\0garbage', b'{"text":"\\u0000"}', b'{"text":"\\ud800"}']:
            response = await self.call(body)
            self.assertEqual(response.status_code, 400, (body, response.text))
            self.assertNotIn('JSON data', response.text)
        self.assertEqual(sql('SELECT last_value FROM pgxio_private.input_calls'), before)
        self.assertEqual(await self.pid(), pid)

    async def test_raw_precision_escapes_and_duplicate_key_semantics(self):
        # PostgreSQL numeric retains values beyond serde_json's u64 integer range.
        value = (await self.call(b' { "n":18446744073709551617, "text":"first", '
                                 b'"text":"\\u0061\\uD83D\\uDE00" } ')).json()
        self.assertEqual(value['number'], '18446744073709551617')
        self.assertEqual(value['text'], 'a😀')

    async def test_valid_scalar_and_array_input_is_sql_owned(self):
        for body, kind in [(b'[]', 'array'), (b'1', 'number'), (b'null', 'null'),
                           (b'true', 'boolean'), (b'"hello"', 'string')]:
            response = await self.call(body)
            self.assertEqual(response.status_code, 400 if SCHEMA else 200, response.text)
            if not SCHEMA:
                self.assertEqual(response.json()['kind'], kind)

    async def test_exact_limit_chunked_body_and_oversize(self):
        for size in (1024, 16384, 65536):
            body = b'{"text":"' + b'x' * (size - 11) + b'"}'
            self.assertEqual(len(body), size)
            async def chunks():
                for offset in range(0, size, 997):
                    yield body[offset:offset + 997]
            response = await self.call(chunks())
            self.assertEqual(response.status_code, 200, (size, response.text[:200]))
            self.assertEqual(len(response.json()['text']), size - 11)
        response = await self.call(b'{"text":"' + b'x' * 65536 + b'"}')
        self.assertEqual(response.status_code, 413)

    async def test_sql_input_errors_rollback_and_hide_diagnostics(self):
        pid = await self.pid()
        before = sql('SELECT count(*) FROM pgxio_private.tasks')
        for mode, extra in [('input_error', {}), ('data_error', {'bad_integer': 'not-an-integer'})]:
            response = await self.call(json.dumps({'marker': 'must rollback', 'mode': mode, **extra}).encode(), 'perf_probe')
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json(), {'error': {'code': 'invalid_input'}})
            self.assertEqual(sql('SELECT count(*) FROM pgxio_private.tasks'), before)
            self.assertEqual(await self.pid(), pid)

    async def test_sql_functions_preserve_example_contract(self):
        for value in [{}, {'title': 42}, {'title': None}, {'title': ''}, {'title': 'x' * 501},
                      {'title': 'valid', 'extra': True}, [], None]:
            response = await self.call(json.dumps(value).encode(), 'create_task')
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual((await self.call(b'{"sql":"select 1"}', 'list_tasks')).status_code, 400)

    async def test_queued_body_has_its_own_storage(self):
        if os.environ.get('PGXIO_TEST_PROFILE') == 'failfast':
            self.skipTest('fail-fast does not queue requests')
        slow = asyncio.create_task(self.call(b'{"delay":0.5}', 'perf_probe'))
        try:
            for _ in range(50):
                active = await asyncio.to_thread(sql, "SELECT count(*) FROM pg_stat_activity WHERE usename='web' AND state='active' AND query LIKE '%perf_probe%'")
                if active != '0': break
                await asyncio.sleep(.01)
            else: self.fail('slow query did not start')
            expected = 'queued 😀 ' + 'x' * 16384
            response = await self.call(json.dumps({'text': expected}).encode())
            self.assertEqual(response.status_code, 200, response.text[:100])
            self.assertEqual(response.json()['text'], expected)
        finally:
            await slow


if __name__ == '__main__':
    unittest.main()

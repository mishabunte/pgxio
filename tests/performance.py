"""Database lifecycle/admission regressions; use tests/run-isolated.py."""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from contextlib import asynccontextmanager
import httpx
from integration import COMPOSE, ROOT, URL, connect, sql, subscribed, token

PROFILE=os.environ.get('PGXIO_TEST_PROFILE','default')

class Performance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http=httpx.AsyncClient(base_url=URL,timeout=10)
        for _ in range(100):
            try:
                if (await self.http.get('/readyz')).status_code==200: break
            except httpx.HTTPError: pass
            await asyncio.sleep(.1)
        else: self.fail('API not ready')
        self.jwt=token()
        self.headers={'Authorization':'Bearer '+self.jwt}
    async def asyncTearDown(self):
        await self.http.aclose()
        await asyncio.sleep(.05)
    async def probe(self,body=None,jwt=None):
        return await self.http.post('/v1/perf_probe',json=body or {},headers={'Authorization':'Bearer '+(self.jwt if jwt is None else jwt)})
    async def active(self):
        for _ in range(100):
            if 'active' in await asyncio.to_thread(sql,"SELECT state FROM pg_stat_activity WHERE usename='web' AND query LIKE '%perf_probe%' AND state='active';"): return
            await asyncio.sleep(.01)
        self.fail('slow query did not become active')
    @asynccontextmanager
    async def held_pool(self):
        # Hold a server-side lock until the assertion finishes; observer latency
        # cannot consume a short pg_sleep window on an emulated platform.
        key=8675309
        process=subprocess.Popen(COMPOSE+['exec','-T','postgres','psql','-U','postgres','-d','pgxio','-At','-v','ON_ERROR_STOP=1'],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        slow=None
        try:
            process.stdin.write(f"SELECT pg_advisory_lock({key}); SELECT 'held';\n");process.stdin.flush()
            for _ in range(4):
                line=await asyncio.wait_for(asyncio.to_thread(process.stdout.readline),10)
                if line.strip()=='held':break
                if not line: self.fail('database barrier exited before acquiring lock')
            else:self.fail('database barrier did not report acquisition')
            slow=asyncio.create_task(self.probe({'gate':key}))
            await self.active()
            yield slow
        finally:
            if process.poll() is None:
                process.stdin.write('\\q\n');process.stdin.flush();process.stdin.close()
                await asyncio.wait_for(asyncio.to_thread(process.wait),10)
            if slow is not None:await slow
            process.stdout.close();process.stderr.close()

    async def test_preparation_and_claims_are_reused_without_tenant_leakage(self):
        first=await self.probe();self.assertEqual(first.status_code,200,first.text)
        pid=first.json()['pid'];count=first.json()['prepared']
        self.assertGreaterEqual(count,2) if PROFILE!='unprepared' else self.assertEqual(count,0)
        for subject in ('bob','alice','bob','alice'):
            response=await self.probe(jwt=token(subject)); self.assertEqual(response.status_code,200,response.text)
            value=response.json();self.assertEqual(value['pid'],pid);self.assertEqual(value['subject'],subject)
            self.assertEqual(value['prepared'],count);self.assertEqual(value['timeout'],'4s');self.assertEqual(value['lock_timeout'],'1s')
        self.assertEqual(sql("SELECT count(*) FROM pg_stat_activity WHERE usename='web';"),'2')
    async def test_errors_and_invalid_output_rollback_and_reuse_backend(self):
        pid=(await self.probe()).json()['pid']
        for mode in (('error','invalid') if os.environ.get('PGXIO_TEST_OUTPUT_VALIDATION') == 'schema' else ('error',)):
            marker='perf-'+mode+'-'+str(time.time_ns())
            response=await self.probe({'mode':mode,'marker':marker});self.assertEqual(response.status_code,503,response.text)
            self.assertEqual(sql("SELECT count(*) FROM pgxio_private.tasks WHERE title='"+marker+"';"),'0')
            good=await self.probe();self.assertEqual(good.status_code,200,good.text);self.assertEqual(good.json()['pid'],pid)
    async def test_repeated_failures_do_not_grow_sessions_or_prepared_registry(self):
        first=(await self.probe()).json()
        for index in range(20):
            response=await self.probe({'mode':'invalid' if index%2 and os.environ.get('PGXIO_TEST_OUTPUT_VALIDATION') == 'schema' else 'error'})
            self.assertEqual(response.status_code,503,response.text)
        final=await self.probe();self.assertEqual(final.status_code,200,final.text)
        self.assertEqual(final.json()['pid'],first['pid']);self.assertEqual(final.json()['prepared'],first['prepared'])
        self.assertEqual(sql("SELECT count(*) FROM pg_stat_activity WHERE usename='web';"),'2')

    async def test_cancelled_write_rolls_back_and_reuses_backend(self):
        pid=(await self.probe()).json()['pid'];marker='perf-cancel-'+str(time.time_ns())
        with self.assertRaises(httpx.ReadTimeout):
            await self.http.post('/v1/perf_probe',json={'marker':marker,'delay':2},headers=self.headers,timeout=.15)
        for _ in range(50):
            response=await self.probe()
            if response.status_code==200: break
            await asyncio.sleep(.05)
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['pid'],pid)
        self.assertEqual(sql("SELECT count(*) FROM pgxio_private.tasks WHERE title='"+marker+"';"),'0')
    async def test_statement_timeout_rolls_back_and_reuses_backend(self):
        pid=(await self.probe()).json()['pid'];marker='perf-timeout-'+str(time.time_ns())
        response=await self.probe({'marker':marker,'delay':5});self.assertEqual(response.status_code,503,response.text)
        good=await self.probe();self.assertEqual(good.status_code,200,good.text);self.assertEqual(good.json()['pid'],pid)
        self.assertEqual(sql("SELECT count(*) FROM pgxio_private.tasks WHERE title='"+marker+"';"),'0')
    @unittest.skipIf(PROFILE=='unprepared','no prepared statements to invalidate')
    async def test_invalidated_statement_fails_once_then_reprepares_on_same_backend(self):
        pid=(await self.probe()).json()['pid']
        response=await self.probe({'mode':'invalidate'});self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((await self.probe()).status_code,503)
        good=await self.probe();self.assertEqual(good.status_code,200,good.text);self.assertEqual(good.json()['pid'],pid)
    async def test_readiness_stays_ready_while_pool_is_busy(self):
        async with self.held_pool() as slow:
            self.assertFalse(slow.done())
            self.assertEqual((await self.http.get('/readyz')).status_code,200)
            self.assertFalse(slow.done())
    async def test_pool_waits_or_rejects_according_to_profile(self):
        async with self.held_pool():
            start=time.monotonic()
            if PROFILE=='failfast':
                response=await self.probe()
                self.assertEqual(response.status_code,503);self.assertLess(time.monotonic()-start,.3)
                return
            waiting=asyncio.create_task(self.probe())
            await asyncio.sleep(.15)
            self.assertFalse(waiting.done(),'request must wait for the occupied pool')
        response=await waiting
        self.assertEqual(response.status_code,200,response.text)
    @unittest.skipIf(PROFILE=='failfast','waiting disabled')
    async def test_pool_timeout_is_bounded_and_does_not_execute(self):
        marker='perf-queued-'+str(time.time_ns())
        async with self.held_pool():
            start=time.monotonic();response=await self.probe({'marker':marker});elapsed=time.monotonic()-start
            self.assertEqual(response.status_code,503,response.text);self.assertEqual(response.json()['error']['code'],'pool_timeout')
            self.assertGreaterEqual(elapsed,.9);self.assertLess(elapsed,1.5)
        self.assertEqual(sql("SELECT count(*) FROM pgxio_private.tasks WHERE title='"+marker+"';"),'0')
    @unittest.skipIf(PROFILE=='failfast','waiting disabled')
    async def test_queued_cancellation_is_removed(self):
        marker='perf-queue-cancel-'+str(time.time_ns())
        async with self.held_pool() as slow:
            self.assertFalse(slow.done())
            with self.assertRaises(httpx.ReadTimeout):
                await self.http.post('/v1/perf_probe',json={'marker':marker},headers=self.headers,timeout=.1)
        self.assertEqual((await self.probe()).status_code,200)
        self.assertEqual(sql("SELECT count(*) FROM pgxio_private.tasks WHERE title='"+marker+"';"),'0')
    @unittest.skipIf(PROFILE=='failfast','waiting disabled')
    async def test_queue_is_bounded_under_overload(self):
        async with self.held_pool():
            tasks=[asyncio.create_task(self.probe()) for _ in range(40)]
            results=await asyncio.gather(*tasks)
        errors=[r.json()['error']['code'] for r in results if r.status_code==503]
        self.assertIn('busy',errors);self.assertIn('pool_timeout',errors)
        self.assertEqual((await self.probe()).status_code,200)
    @unittest.skipIf(PROFILE=='failfast','waiting disabled')
    async def test_expired_queued_token_cannot_execute(self):
        marker='perf-expired-'+str(time.time_ns())
        async with self.held_pool() as slow:
            jwt=token(seconds=1);payload=jwt.split('.')[1]
            expiry=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))['exp']
            await asyncio.sleep(max(0,expiry-time.time()-.25))
            self.assertGreater(expiry-time.time(),0,'token must be valid when submitted')
            self.assertFalse(slow.done())
            response=await self.probe({'marker':marker},jwt=jwt)
        self.assertEqual(response.status_code,401,response.text)
        self.assertEqual(sql("SELECT count(*) FROM pgxio_private.tasks WHERE title='"+marker+"';"),'0')
    async def test_websocket_backpressure_preserves_fanout_and_http(self):
        if PROFILE=='failfast': self.skipTest('HTTP waiting disabled')
        sockets=[]
        notifications=None
        try:
            for _ in range(40): sockets.append(await subscribed())
            response=await self.http.post('/v1/create_task',json={'title':'fair fanout'},headers=self.headers)
            self.assertEqual(response.status_code,200,response.text)
            notifications=asyncio.gather(*(asyncio.wait_for(ws.recv(),5) for ws in sockets))
            # Forty invalidations can fill the bounded 32-job queue before this
            # HTTP request arrives. Busy is valid; starvation is not.
            deadline=time.monotonic()+5
            while True:
                http=await self.probe()
                if http.status_code==200: break
                self.assertEqual((http.status_code,http.json()),(503,{'error':{'code':'busy'}}))
                self.assertLess(time.monotonic(),deadline,'HTTP did not recover after fanout')
                await asyncio.sleep(.02)
            messages=await notifications;self.assertTrue(all(json.loads(message)['type']=='changed' for message in messages))
        finally:
            if notifications is not None:
                if not notifications.done(): notifications.cancel()
                await asyncio.gather(notifications,return_exceptions=True)
            await asyncio.gather(*(ws.close() for ws in sockets))

    async def test_reconnect_reaudits_and_rebuilds_prepared_cache(self):
        pid=(await self.probe()).json()['pid'];sql(f'SELECT pg_terminate_backend({pid});')
        for _ in range(60):
            response=await self.probe()
            if response.status_code==200 and response.json()['pid']!=pid: break
            await asyncio.sleep(.1)
        self.assertEqual(response.status_code,200,response.text);self.assertNotEqual(response.json()['pid'],pid)
        if PROFILE!='unprepared': self.assertGreaterEqual(response.json()['prepared'],2)
    @unittest.skipUnless(os.environ.get('PGXIO_TEST_RSA_KEY'),'requires isolated production JWKS')
    async def test_key_reload_invalidates_cached_token(self):
        path=Path(os.environ['PGXIO_TEST_COMPOSE_FILE']).parent/'runtime/jwks.json'
        original=path.read_text()
        self.assertEqual((await self.probe()).status_code,200)
        try:
            jwks=json.loads(original);jwks['keys'][0]['kid']='replacement-key'
            path.write_text(json.dumps(jwks))
            subprocess.run(COMPOSE+['exec','-T','api','nginx','-c','/etc/nginx/nginx.conf','-s','reload'],cwd=ROOT,check=True,capture_output=True)
            for _ in range(60):
                async with httpx.AsyncClient(base_url=URL,timeout=5) as client:
                    response=await client.post('/v1/perf_probe',json={},headers=self.headers)
                if response.status_code==401: break
                await asyncio.sleep(.1)
            self.assertEqual(response.status_code,401,response.text)
        finally:
            path.write_text(original)
            subprocess.run(COMPOSE+['exec','-T','api','nginx','-c','/etc/nginx/nginx.conf','-s','reload'],cwd=ROOT,check=True,capture_output=True)
            await asyncio.sleep(.3)

    async def test_jwt_cache_never_accepts_bad_signature_or_expired_token(self):
        self.assertEqual((await self.probe()).status_code,200)
        self.assertEqual((await self.probe(jwt=self.jwt[:-8]+'AAAAAAAA')).status_code,401)
        self.assertEqual((await self.probe(jwt=token(seconds=-1))).status_code,401)
        fresh=token(seconds=2);self.assertEqual((await self.probe(jwt=fresh)).status_code,200)
        await asyncio.sleep(2.1);self.assertEqual((await self.probe(jwt=fresh)).status_code,401)

if __name__=='__main__': unittest.main(verbosity=2)

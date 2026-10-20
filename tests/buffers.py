"""Test-only buffer-selection telemetry; absent from production builds."""
import asyncio
import json
import os
import unittest
import httpx
from integration import URL, token


class Buffers(unittest.IsolatedAsyncioTestCase):
    async def test_effective_selection_matches_mode_and_transport(self):
        mode=os.environ.get('PGXIO_TEST_BUFFER_MODE','copy')
        async with httpx.AsyncClient(base_url=URL,timeout=10) as client:
            stats=None
            for _ in range(100):
                reply=await client.get('/__pgxio_buffers')
                if reply.status_code in (404,405):
                    self.assertNotEqual(os.environ.get('PGXIO_TEST_DIAGNOSTICS'),'1','required selection telemetry is missing')
                    self.skipTest('selection telemetry requires benchmark-auth build')
                self.assertEqual(reply.status_code,200)
                stats=reply.json()
                if stats['plaintext'] or os.environ.get('PGXIO_TEST_HTTP2')=='1': break
                await asyncio.sleep(.05)
            for size in (1024,16384,65536):
                raw=json.dumps({'padding':''},separators=(',',':')).encode()
                body=json.dumps({'padding':'x'*(size-len(raw))},separators=(',',':')).encode()
                before=(await client.get('/__pgxio_buffers')).json()
                response=await client.post('/v1/input_probe',content=body,
                    headers={'Authorization':'Bearer '+token(),'Content-Type':'application/json'})
                self.assertEqual(response.status_code,200,response.text)
                after=(await client.get('/__pgxio_buffers')).json()
                schema=os.environ.get('PGXIO_TEST_INPUT_VALIDATION')=='schema'
                if mode=='copy' or schema or os.environ.get('PGXIO_TEST_PATCHED')!='1' or (mode=='auto' and (not before['plaintext'] or size<16384)):
                    self.assertEqual(after['copy']-before['copy'],1)
                    self.assertEqual(after['borrowed']-before['borrowed'],0)
                elif mode=='borrowed':
                    self.assertEqual(after['borrowed']-before['borrowed'],1)
                else:
                    # Actual Nginx fragmentation can conservatively select copy.
                    self.assertEqual(after['copy']+after['borrowed']-before['copy']-before['borrowed'],1)
                self.assertEqual(after['auto_copied'],before['auto_copied'])


if __name__=='__main__':unittest.main()
